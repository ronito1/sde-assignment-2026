"""The ONE cohort object (C2). Detection, experiments, schedules, the supervisor view and the audit all refer
to cohorts by these ids and these `where` clauses, evaluated with the simulator's documented semantics
(first match wins; AND across features, OR within a feature's list; everything else is `default`).

Cohorts are chosen once per workflow from pre-D0 history (C3): we look for the lead feature whose values
have the most different time-of-day pickup profiles, and give each sizeable value its own cohort. Few and
coarse on purpose: every cohort must be able to fill an experiment arm within about two days.
"""
from __future__ import annotations

import json

import numpy as np

from . import settings
from .model import FEATURES, Model


def registry(m: Model, wid: str) -> list[dict]:
    rows = m.s.q("SELECT cid, where_json, owner, basis FROM cohorts WHERE wid=? ORDER BY ord", (wid,))
    return [{"id": c, "where": json.loads(w), "owner": o, "basis": json.loads(b) if b else None}
            for c, w, o, b in rows]


def fc_assign(m: Model, cohorts: list[dict]) -> np.ndarray:
    """fc -> cohort index (0 = default, i+1 = cohorts[i])."""
    mat = m.fc_matrix()
    n = len(m.fc_feat)
    out = np.zeros(n, dtype=np.int16)
    free = np.ones(n, dtype=bool)
    for i, c in enumerate(cohorts):
        mask = free.copy()
        for feat, vals in c["where"].items():
            mask &= np.isin(mat[feat], vals)
        out[mask] = i + 1
        free &= ~mask
    return out


def call_cohorts(m: Model, cohorts: list[dict], idx: np.ndarray) -> np.ndarray:
    """Cohort index of each call in idx."""
    return fc_assign(m, cohorts)[m.calls["fc"][idx]]


def hour_of(minutes: np.ndarray) -> np.ndarray:
    return (minutes % 1440) // 60


def choose(m: Model, wid: str) -> list[dict]:
    """Pick cohorts from history for a workflow; persisted so ids are stable across restarts."""
    existing = registry(m, wid)
    if existing:
        return existing
    w = m.wcode(wid)
    C = m.calls
    idx = np.flatnonzero((C["w"] == w) & C["hist"])
    if len(idx) < 2000:
        idx = np.flatnonzero(C["w"] == w)
    lo, hi = m.wf[wid]["dialling_window"]
    hrs = hour_of(C["minute"][idx]) - lo
    H = hi - lo
    conn = C["conn"][idx]
    fc = C["fc"][idx]
    mat = m.fc_matrix()
    best = None
    n_all = np.bincount(hrs, minlength=H).astype(float)
    hour_eff = (np.bincount(hrs, weights=conn, minlength=H) / np.maximum(n_all, 1))
    hour_eff = hour_eff / max(float(conn.mean()), 1e-9)          # pooled hour-of-day effect on pickup
    for feat in FEATURES:
        vals = mat[feat][fc]
        uniq = [v for v in sorted(set(vals.tolist())) if v not in (None, "None")]
        if len(uniq) < 2:
            continue
        # chi-square for a value x hour interaction: observed connects vs "value rate x pooled hour effect"
        chi2, df, shares = 0.0, 0, {}
        for v in uniq:
            sel = vals == v
            n_h = np.bincount(hrs[sel], minlength=H).astype(float)
            c_h = np.bincount(hrs[sel], weights=conn[sel], minlength=H)
            r_v = c_h.sum() / max(n_h.sum(), 1)
            e_h = np.clip(n_h * r_v * hour_eff, 1e-9, None)
            ok = n_h >= 20
            chi2 += float((((c_h - e_h) ** 2) / (e_h * np.clip(1 - e_h / np.maximum(n_h, 1), 0.05, 1)))[ok].sum())
            df += int(ok.sum())
            shares[v] = n_h.sum() / len(idx)
        df = max(df - H - len(uniq) + 1, 1)
        z = (chi2 - df) / np.sqrt(2 * df)
        if best is None or z > best[0]:
            best = (float(z), feat, None, shares)
    cohorts = []
    if best and best[0] > 4.0:   # interaction chi-square more than 4 sd above its no-interaction mean
        _, feat, _, shares = best
        big = sorted([v for v, s in shares.items() if s >= settings.MIN_COHORT_SHARE], key=lambda v: -shares[v])
        for v in big[: settings.MAX_COHORTS]:
            cohorts.append({"id": f"{feat}_{_slug(v)}", "where": {feat: [v]}, "owner": "controller",
                            "basis": {"feature": feat, "share": round(shares[v], 3),
                                      "interaction_z": round(best[0], 1)}})
    with m.s.tx():
        for i, c in enumerate(cohorts):
            m.s.db.execute("INSERT OR IGNORE INTO cohorts VALUES(?,?,?,?,?,?,?)",
                           (wid, c["id"], i, json.dumps(c["where"]), c["owner"], json.dumps(c["basis"]), m.now))
        m.s.decide(m.now, wid, "cohorts_chosen",
                   f"{len(cohorts)} cohorts from history" + (f" on {best[1]}" if cohorts else " (no strong signal)"),
                   {"cohorts": cohorts, "candidates_signal": best[0] if best else None})
    return cohorts


def adopt_supervisor_cohorts(m: Model, wid: str, config: dict):
    """Cohorts a supervisor added become registry members we never delete."""
    have = {c["id"] for c in registry(m, wid)}
    new = [c for c in config.get("cohorts", []) if c["id"] not in have]
    if not new:
        return
    base = len(have)
    with m.s.tx():
        for i, c in enumerate(new):
            m.s.db.execute("INSERT OR IGNORE INTO cohorts VALUES(?,?,?,?,?,?,?)",
                           (wid, c["id"], base + i, json.dumps(c["where"]), "supervisor", None, m.now))
        m.s.decide(m.now, wid, "cohorts_adopted", f"adopted {len(new)} supervisor cohort(s)", {"cohorts": new})


def merged_cohorts(current_cfg: dict, ours: list[dict]) -> list[dict]:
    """Config cohort list: whatever is already in the config keeps its place (a supervisor's order wins),
    then any of ours that are missing, capped at 20."""
    out = [{"id": c["id"], "where": c["where"]} for c in current_cfg.get("cohorts", [])]
    have = {c["id"] for c in out}
    for c in ours:
        if c["id"] not in have and len(out) < 20:
            out.append({"id": c["id"], "where": c["where"]})
    return out


def _slug(v) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in str(v)).strip("_").lower() or "x"
