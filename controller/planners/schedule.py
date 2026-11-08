"""Call-time optimisation per cohort (C4, partial: no per-person preferred hours).

`hour_weights` only decide WHO the dialler picks in an hour, never how many calls it makes, so only the
difference between cohorts' hour profiles matters. From pre-D0 history (dialled with no schedule, i.e. a
fair sample of hours) we estimate each cohort's pickup in each hour relative to its own average, shrink it
toward the pooled hour effect, sharpen it, and clip it to [0.3, 3]. Every hour in the window keeps a
positive weight: a missing hour would be weight 0 and could leave capacity unused.
"""
from __future__ import annotations

import numpy as np

from .. import cohorts as cohorts_mod
from .. import settings
from ..model import Model
from . import active_leads, backoff_ok, idx, inflight_keys, intent, set_backoff

KEY = "schedule"


class SchedulePlanner:
    kind = "schedule"

    def plan(self, m: Model, wid: str, gov) -> list[dict]:
        if m.now < 0 or KEY in inflight_keys(m, wid) or not backoff_ok(m, wid, KEY):
            return []
        if m.s.get(f"schedule_done:{wid}"):
            return []
        ours = [c for c in cohorts_mod.registry(m, wid) if c["owner"] == "controller"]
        if not ours:
            return []
        tables, est = self.weights(m, wid, ours)
        if est["gain"] < settings.SCHEDULE_MIN_GAIN:
            with m.s.tx():
                m.s.put(f"schedule_done:{wid}", {"skipped": est})
                m.s.decide(m.now, wid, "schedule_skipped", f"estimated pickup gain {est['gain']:+.1%} too small", est)
            return []
        ops = [("cohorts", ours)] + [("hour_weights", cid, t) for cid, t in tables.items()]
        wf = m.wf[wid]
        dims = ["cohorts", "schedule"]
        cur = m.cfg[wid]["config"]
        if all(c["id"] in {x["id"] for x in cur.get("cohorts", [])} for c in ours):
            dims = ["schedule"]
        return [intent(KEY, self.kind, ops, dims, "routine", auto_ok=True,
                       summary=f"Call each of {len(ours)} customer groups in its better-answering hours",
                       diff={"cohorts": [c["id"] for c in ours], "hour_weights": tables},
                       rationale=(f"History shows these groups answer at different times "
                                  f"(interaction z={ours[0]['basis']['interaction_z'] if ours[0].get('basis') else '?'}). "
                                  f"Weights only reorder who is called each hour; dial volume is unchanged."),
                       affected=active_leads(m, wid), impact=min(1.0, est["gain"] / 0.10) * 0.8,
                       dry_run={"method": "replayed pre-D0 hour x cohort pickup with the proposed weights",
                                **est},
                       evidence={"basis": [c.get("basis") for c in ours], "window": wf["dialling_window"]})]

    def weights(self, m: Model, wid: str, ours: list[dict]):
        C = m.calls
        ix = idx(m, wid, hist=True)
        if len(ix) < 2000:
            ix = idx(m, wid, hist=None)
        lo, hi = m.wf[wid]["dialling_window"]
        H = hi - lo
        hrs = (C["minute"][ix] % 1440) // 60 - lo
        ok = (hrs >= 0) & (hrs < H)
        ix, hrs = ix[ok], hrs[ok]
        conn = C["conn"][ix].astype(float)
        coh = cohorts_mod.call_cohorts(m, ours, ix)
        n_all = np.bincount(hrs, minlength=H).astype(float)
        pool = np.bincount(hrs, weights=conn, minlength=H) / np.maximum(n_all, 1)
        pool_prof = pool / max(conn.mean(), 1e-9)
        names = ["default"] + [c["id"] for c in ours]
        tables, prof, shares, rates = {}, {}, {}, {}
        k = settings.SCHEDULE_PRIOR_DIALS
        for ci, name in enumerate(names):
            sel = coh == ci
            if not sel.any():
                continue
            n_h = np.bincount(hrs[sel], minlength=H).astype(float)
            c_h = np.bincount(hrs[sel], weights=conn[sel], minlength=H)
            r = c_h.sum() / max(n_h.sum(), 1)
            p = ((c_h + k * r * pool_prof) / (n_h + k)) / max(r, 1e-9)    # shrunk profile, mean ~1
            w = np.clip(p ** settings.SCHEDULE_GAMMA, *settings.SCHEDULE_CLIP)
            tables[name] = {str(lo + h): round(float(w[h]), 3) for h in range(H)}
            prof[name], shares[name], rates[name] = p, sel.mean(), r
        # dry run: per hour, pickup of the mix the dialler would pick (eligible mix ~ cohort shares)
        base, new = [], []
        for h in range(H):
            num_u = sum(shares[n] * rates[n] * prof[n][h] for n in prof)
            den_u = sum(shares[n] for n in prof)
            num_w = sum(shares[n] * tables[n][str(lo + h)] * rates[n] * prof[n][h] for n in prof)
            den_w = sum(shares[n] * tables[n][str(lo + h)] for n in prof)
            base.append(num_u / den_u)
            new.append(num_w / den_w)
        gain = float(np.mean(new) / max(np.mean(base), 1e-9) - 1)
        est = {"gain": round(gain, 4), "pickup_before": round(float(np.mean(base)), 4),
               "pickup_after": round(float(np.mean(new)), 4), "dials_used": int(len(ix)),
               "caveat": "assumes the eligible pool mix matches cohort shares; real gain is smaller as good "
                         "hours deplete"}
        if "default" in tables and "default" not in [c["id"] for c in ours]:
            pass
        return tables, est

    def applied(self, m: Model, wid: str, it: dict, version: int):
        m.s.put(f"schedule_done:{wid}", {"version": version, "minute": m.now})
        m.s.decide(m.now, wid, "schedule_live", f"call-time weights live in v{version}", it["dry_run"])

    def failed(self, m: Model, wid: str, it: dict, state: str):
        set_backoff(m, wid, KEY, state)
