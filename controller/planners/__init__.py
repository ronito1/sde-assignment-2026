"""Planners turn evidence into intents. Each one owns a slice of the config and a small state machine.

Interface:
    plan(m, wid, gov) -> list[intent]     called every loop iteration (cheap ones) or hourly
    applied(m, wid, intent, version)      called inside the transaction that records the apply
    failed(m, wid, intent, state)         called inside the transaction that records the failure
"""
from __future__ import annotations

import json

import numpy as np

from .. import policy
from ..model import Model

MIN_PER_DAY = 1440


def idx(m: Model, wid: str, since: int | None = None, until: int | None = None, hist: bool | None = False):
    C = m.calls
    mask = C["w"] == m.wcode(wid)
    if hist is not None:
        mask &= C["hist"] == hist
    if since is not None:
        mask &= C["minute"] >= since
    if until is not None:
        mask &= C["minute"] < until
    return np.flatnonzero(mask)


def counts_by(m: Model, ix: np.ndarray, col: str) -> dict[str, dict]:
    """value name -> {dials, conn, eng, hf} for the calls in ix."""
    C = m.calls
    v = C[col][ix]
    out = {}
    if not len(ix):
        return out
    for code in np.unique(v):
        sel = ix[v == code]
        out[m.s.name(code)] = {"dials": int(len(sel)), "conn": int(C["conn"][sel].sum()),
                               "eng": int(C["eng"][sel].sum()), "hf": int(C["hf"][sel].sum())}
    return out


def inflight_keys(m: Model, wid: str) -> set:
    keys = set()
    for (intents,) in m.s.q("SELECT intents FROM proposals WHERE wid=? AND state IN ('PREPARED','PENDING','APPROVED')",
                            (wid,)):
        keys |= {i["key"] for i in json.loads(intents)}
    return keys


def backoff_ok(m: Model, wid: str, key: str) -> bool:
    return m.now >= m.s.get(f"backoff:{wid}:{key}", -10 ** 9)


def set_backoff(m: Model, wid: str, key: str, state: str):
    # An expired / stale / lapsed proposal is re-planned from fresh data at once (Product memo); a human
    # rejection is respected for half a day before the same idea is offered again.
    if state == "REJECTED":
        m.s.put(f"backoff:{wid}:{key}", m.now + 12 * 60)


def intent(key, kind, ops, dims, urgency, auto_ok, summary, diff, rationale, *, affected=0, dry_run=None,
           impact=0.0, evidence=None, meta=None, validation_run_id=None):
    return {"key": key, "kind": kind, "ops": ops, "dims": dims, "urgency": urgency, "auto_ok": auto_ok,
            "summary": summary, "diff": diff, "rationale": rationale, "affected": int(affected),
            "dry_run": dry_run or {"note": "no replay applicable"}, "impact": float(impact),
            "evidence": evidence or {}, "meta": meta or {}, "validation_run_id": validation_run_id}


def active_leads(m: Model, wid: str, fc_mask: np.ndarray | None = None) -> int:
    """Leads still dialable in the next 24h (+ the next batch), optionally restricted to an fc mask."""
    wf = m.wf[wid]
    horizon = m.now - len(wf["cadence"]) * MIN_PER_DAY
    rows = m.s.q("SELECT fc, COUNT(*) FROM leads WHERE wid=? AND minute>=? GROUP BY fc", (wid, horizon))
    ix = idx(m, wid, since=horizon)
    C = m.calls
    eng = np.bincount(C["fc"][ix][C["eng"][ix]], minlength=len(m.fc_feat))
    total, share_num, share_den = 0, 0, 0
    for fc, n in rows:
        keep = 1 if fc_mask is None or (fc < len(fc_mask) and fc_mask[fc]) else 0
        live = max(n - int(eng[fc]) if fc < len(eng) else n, 0)
        share_den += n
        share_num += n * keep
        if wf["mid_flight"] == "allowed":
            total += live * keep
    share = share_num / share_den if share_den else 1.0
    return int(total + wf["daily_batch"]["size"] * share)


def stepped(m: Model, wid: str, cohort: str, dim: str, target: dict, auto: bool) -> dict:
    """Respect max_shift when the change will be auto-applied; an approval can move the whole way."""
    if not auto:
        return target
    cur = policy.dist(m.cfg[wid]["config"], cohort, dim)
    return policy.step_towards(cur, target, m.wf[wid]["governance"]["auto_apply"].get("max_shift", 0.0))
