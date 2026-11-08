"""Local preflight of what the simulator's governance will accept, so we never burn a request (or log a
403 auto-apply attempt) on something we could have known was invalid.

This is an independent implementation of the rules written in docs/SIMULATOR.md §3-§4, not an import of
`sim` (which would be disqualifying).
"""
from __future__ import annotations

import copy

from .stats import tv

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
CHANGE_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def _norm(d: dict) -> dict:
    s = float(sum(d.values()))
    return {k: v / s for k, v in d.items() if v > 0}


def normalised(cfg: dict) -> dict:
    """Normalise weights the way the server does, so diffs compare like with like."""
    out = copy.deepcopy(cfg)
    out.setdefault("cohorts", [])
    out.setdefault("schedule", {})
    out["schedule"].setdefault("hour_weights", {})
    out["schedule"].setdefault("lead_preferred_hour", {})
    for key, dims in out["allocations"].items():
        for dim, w in list(dims.items()):
            dims[dim] = _norm(w)
    for key, hw in out["schedule"]["hour_weights"].items():
        out["schedule"]["hour_weights"][key] = {str(int(h)): float(v) for h, v in hw.items()}
    return out


def changed_dims(old: dict, new: dict) -> list[str]:
    old, new = normalised(old), normalised(new)
    ch = set()
    if old["cohorts"] != new["cohorts"]:
        ch.add("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for d in AGENT_DIMS:
        if any(_close(dist(old, k, d), dist(new, k, d)) is False for k in keys):
            ch.add(d)
    if old["prompt_version"] != new["prompt_version"]:
        ch.add("prompt_version")
    if old["schedule"] != new["schedule"]:
        ch.add("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def _close(a: dict, b: dict) -> bool:
    if set(a) != set(b):
        return False
    return all(abs(a[k] - b[k]) < 1e-12 for k in a)


def max_shift(old: dict, new: dict) -> float:
    old, new = normalised(old), normalised(new)
    keys = {"default"} | {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]}
    return max((tv(dist(old, k, d), dist(new, k, d)) for k in keys for d in AGENT_DIMS), default=0.0)


def validate(wf: dict, cfg: dict) -> list[str]:
    """Problems the server would reject the config for (422 invalid_config)."""
    p = []
    ids = [c["id"] for c in cfg.get("cohorts", [])]
    if len(ids) != len(set(ids)) or "default" in ids:
        p.append("cohort ids must be unique and not 'default'")
    if len(ids) > 20:
        p.append("at most 20 cohorts")
    allocs = cfg.get("allocations", {})
    if "default" not in allocs or any(d not in allocs["default"] for d in AGENT_DIMS):
        p.append("allocations.default must define every agent dimension")
    for key, dims in allocs.items():
        if key != "default" and key not in ids:
            p.append(f"allocation for undefined cohort {key}")
        for d, w in dims.items():
            if any(o not in wf["options"][d] for o in w) or sum(w.values()) <= 0:
                p.append(f"bad allocation {key}.{d}")
    if cfg.get("prompt_version") not in wf["options"]["prompt_version"]:
        p.append("prompt not allowed")
    lo, hi = wf["dialling_window"]
    for key, hw in (cfg.get("schedule") or {}).get("hour_weights", {}).items():
        if key != "default" and key not in ids:
            p.append(f"hour_weights for undefined cohort {key}")
        for h, w in hw.items():
            if not lo <= int(h) < hi or w < 0:
                p.append(f"hour {h} outside window or negative weight")
    return p


def auto_apply_problems(wf: dict, old: dict, new: dict) -> list[str]:
    pol = wf["governance"]["auto_apply"]
    p = []
    dims = changed_dims(old, new)
    if not pol.get("enabled"):
        p.append("auto-apply disabled")
    bad = [d for d in dims if d not in pol.get("allowed_dims", [])]
    if bad:
        p.append(f"dims not allowed: {bad}")
    if max_shift(old, new) > pol.get("max_shift", 0) + 1e-9:
        p.append("shift too large")
    if not dims:
        p.append("no change")
    return p


def can_auto(wf: dict, dims) -> bool:
    pol = wf["governance"]["auto_apply"]
    return bool(pol.get("enabled")) and all(d in pol.get("allowed_dims", []) for d in dims)


def step_towards(old: dict, target: dict, limit: float) -> dict:
    """Move a distribution from old toward target by at most `limit` total-variation distance."""
    d = tv(old, target)
    if d <= limit + 1e-12:
        return dict(target)
    f = limit / d * 0.999
    keys = set(old) | set(target)
    out = {k: old.get(k, 0.0) + f * (target.get(k, 0.0) - old.get(k, 0.0)) for k in keys}
    return {k: round(v, 6) for k, v in out.items() if v > 1e-6}
