"""Caller-ID health (C5).

Health of a DID is measured as its pickup relative to the OTHER DIDs of the same workflow dialled in the
same hours (hour-stratified expected connects), so time-of-day and lead mix cancel out. Two loops:

- detection (every iteration): a DID with ratio < 0.8, >= 250 dials in the last 6h and z < -3 is flagged;
  traffic is moved off it at once (a safety change: auto-applied where the policy allows, otherwise an
  incident-priority approval), keeping a 3% probe so recovery is visible.
- rebalancing (every 6h): untested DIDs get a 3% probe; tested healthy DIDs share traffic in proportion to
  their health. Spreading load matters: a DID that carries all the traffic wears out.
"""
from __future__ import annotations

import numpy as np

from .. import policy, settings
from ..model import Model
from ..stats import normalise, tv
from . import active_leads, backoff_ok, idx, inflight_keys, intent, set_backoff, stepped


def health(m: Model, wid: str, since: int, until: int | None = None, prior: float = 0.0) -> dict:
    C = m.calls
    ix = idx(m, wid, since=since, until=until)
    out = {}
    if not len(ix):
        return out
    hrs = (C["minute"][ix] % 1440) // 60
    ci = C["ci"][ix]
    conn = C["conn"][ix]
    for code in np.unique(ci):
        mine = ci == code
        exp = 0.0
        var = 0.0
        for h in np.unique(hrs[mine]):
            oth = (~mine) & (hrs == h)
            if oth.sum() < 30:
                oth = ~mine
            r = conn[oth].mean() if oth.any() else conn.mean()
            k = int((mine & (hrs == h)).sum())
            exp += k * r
            var += k * r * (1 - r)
        obs = int(conn[mine].sum())
        n = int(mine.sum())
        ratio = (obs + prior) / (exp + prior) if exp + prior > 0 else 1.0
        z = (obs - exp) / np.sqrt(var) if var > 0 else 0.0
        out[m.s.name(code)] = {"dials": n, "conn": obs, "expected": round(exp, 1), "ratio": round(ratio, 3),
                               "z": round(float(z), 2)}
    return out


class CallerIdPlanner:
    kind = "caller_id"

    def plan(self, m: Model, wid: str, gov) -> list[dict]:
        if m.now < 0:
            return []
        wf = m.wf[wid]
        lo, hi = wf["dialling_window"]
        if not lo * 60 <= m.now % 1440 < hi * 60 + 60:
            return []
        keys = inflight_keys(m, wid)
        flagged = set(m.s.get(f"did_flagged:{wid}", []))
        h6 = health(m, wid, m.now - settings.DID_WINDOW_MIN)
        newly = [d for d, h in h6.items() if d not in flagged and h["dials"] >= settings.DID_MIN_DIALS
                 and h["ratio"] < settings.DID_BAD_RATIO and h["z"] < settings.DID_BAD_Z]
        if newly:
            with m.s.tx():
                flagged |= set(newly)
                m.s.put(f"did_flagged:{wid}", sorted(flagged))
                for d in newly:
                    m.s.decide(m.now, wid, "did_flagged", f"caller ID {d} answering at {h6[d]['ratio']:.0%} of "
                               f"its peers over {h6[d]['dials']} dials (z={h6[d]['z']})", h6[d])
        # recovery: a flagged DID whose probe traffic looks healthy again over a day is unflagged
        if flagged:
            h24 = health(m, wid, m.now - 2 * 1440)
            back = [d for d in flagged if d in h24 and h24[d]["dials"] >= 150 and h24[d]["ratio"] >= 0.95
                    and m.s.get(f"did_flag_minute:{wid}:{d}", m.now) < m.now - 1440]
            if back:
                with m.s.tx():
                    flagged -= set(back)
                    m.s.put(f"did_flagged:{wid}", sorted(flagged))
                    m.s.decide(m.now, wid, "did_recovered", f"caller ID(s) {back} healthy again", h24)
        for d in newly:
            with m.s.tx():
                m.s.put(f"did_flag_minute:{wid}:{d}", m.now)
        cur = policy.dist(m.cfg[wid]["config"], "default", "caller_id")
        auto = policy.can_auto(wf, ["caller_id"])
        # ---- safety path: move off flagged DIDs
        bad_share = sum(cur.get(d, 0) for d in flagged)
        if flagged and bad_share > settings.DID_PROBE_SHARE * len(flagged) + 1e-6 and "did:flag" not in keys:
            target = self._target(m, wid, cur, flagged, probe_flagged=True)
            if target and tv(cur, target) > 1e-6:
                step = stepped(m, wid, "default", "caller_id", target, auto)
                ev = {d: h6.get(d) for d in flagged}
                return [intent("did:flag", self.kind, [("alloc", "default", "caller_id", step)], ["caller_id"],
                               "safety" if auto else "incident", auto_ok=True,
                               summary=f"Move calls off unhealthy caller ID(s) {sorted(flagged)}",
                               diff={"caller_id": {"from": cur, "to": step}},
                               rationale="Pickup on these numbers collapsed against peers dialled in the same hours "
                                         "(population test, not single calls). A 3% probe stays to see recovery.",
                               affected=active_leads(m, wid), impact=0.9,
                               dry_run=self._dry(m, wid, cur, step), evidence=ev,
                               meta={"target": target})]
        # ---- routine path: probe untested DIDs and spread load across healthy ones
        if "did:rebalance" in keys or "did:flag" in keys or not backoff_ok(m, wid, "did:rebalance"):
            return []
        last = m.s.get(f"did_rebalance_at:{wid}", -10 ** 9)
        cool = settings.DID_REBALANCE_EVERY_MIN * (4 if wf["mid_flight"] != "allowed" else 1)
        if m.now - last < cool:
            return []
        target = self._target(m, wid, cur, flagged, probe_flagged=True)
        if not target:
            return []
        est_cur = self._exp_health(m, wid, cur)
        est_new = self._exp_health(m, wid, target)
        new_probes = [d for d in target if d not in cur]
        if not new_probes and est_new - est_cur < settings.DID_MIN_EXP_GAIN * max(est_cur, 1e-9):
            return []
        step = stepped(m, wid, "default", "caller_id", target, auto)
        with m.s.tx():
            m.s.put(f"did_rebalance_at:{wid}", m.now)
        h24 = health(m, wid, m.now - settings.DID_PLAN_WINDOW_MIN, prior=60)
        return [intent("did:rebalance", self.kind, [("alloc", "default", "caller_id", step)], ["caller_id"],
                       "routine", auto_ok=True,
                       summary=("Probe unused caller ID(s) " + ", ".join(new_probes) + " at 3%; "
                                if new_probes else "") + "spread calls across healthy caller IDs",
                       diff={"caller_id": {"from": cur, "to": step}},
                       rationale="Caller IDs wear out under load; healthy numbers share traffic in proportion to "
                                 "measured health, untested numbers get a small probe before any real share.",
                       affected=active_leads(m, wid), impact=0.3,
                       dry_run=self._dry(m, wid, cur, step), evidence=h24, meta={"target": target})]

    def _ratios(self, m, wid) -> dict:
        h = health(m, wid, m.now - settings.DID_PLAN_WINDOW_MIN, prior=60)
        return {d: v["ratio"] for d, v in h.items() if v["dials"] >= 100}

    def _proven(self, m, wid) -> dict:
        """DIDs whose health is established: lower 90% bound of the pickup ratio >= 0.9."""
        h = health(m, wid, m.now - 2 * settings.DID_PLAN_WINDOW_MIN)
        out = {}
        for d, v in h.items():
            if v["expected"] <= 0:
                continue
            se = (v["expected"] * (1 - v["expected"] / max(v["dials"], 1))) ** 0.5 / v["expected"]
            if v["ratio"] - 1.2816 * se >= 0.9:
                out[d] = v["ratio"]
        return out

    def _target(self, m, wid, cur, flagged, probe_flagged=True) -> dict:
        opts = m.wf[wid]["options"]["caller_id"]
        r = self._ratios(m, wid)
        proven = self._proven(m, wid)
        in_use = {d for d, p in cur.items() if p > settings.DID_PROBE_SHARE + 1e-6}
        # a DID keeps a real share if it is already carrying traffic and looks fine, or has proven itself
        healthy = {d: r[d] for d in opts if d in r and d not in flagged and r[d] >= 0.9 and (d in in_use or d in proven)}
        if not healthy:
            healthy = {d: 1.0 for d in cur if d not in flagged}
        if not healthy:
            return {}
        t = {}
        probes = settings.DID_PROBE_SHARE
        for d in opts:
            if d not in healthy:
                t[d] = probes          # untested, still being probed, weak, or flagged: watch, don't load
        rest = max(1.0 - sum(t.values()), 0.5)
        # near-equal spread: a number's health falls with its own load, so concentration wears out the winner
        w = normalise({d: min(max(v, 0.5), 1.2) for d, v in healthy.items()})
        for d, v in w.items():
            t[d] = rest * v
        # cap any single number; hand the excess back to the least-bad others in proportion to measured health
        over = sum(max(v - settings.DID_MAX_SHARE, 0) for v in t.values())
        others = {d: max(r.get(d, 0.5), 0.05) for d in t if t[d] < settings.DID_MAX_SHARE and d not in flagged}
        if over > 0 and others:
            for d in t:
                t[d] = min(t[d], settings.DID_MAX_SHARE)
            tot = sum(others.values())
            for d, v in others.items():
                t[d] += over * v / tot
        t = {d: round(v, 4) for d, v in normalise(t).items() if v > 0}
        return t

    def _exp_health(self, m, wid, dist) -> float:
        r = self._ratios(m, wid)
        return sum(p * r.get(d, 0.9) for d, p in dist.items())

    def _dry(self, m, wid, cur, new) -> dict:
        r = self._ratios(m, wid)
        return {"method": "last 24h hour-stratified pickup ratio per caller ID applied to the new split",
                "ratios": r, "expected_relative_pickup_before": round(self._exp_health(m, wid, cur), 4),
                "expected_relative_pickup_after": round(self._exp_health(m, wid, new), 4)}

    def applied(self, m: Model, wid: str, it: dict, version: int):
        m.s.decide(m.now, wid, "caller_id_live", f"caller-ID split live in v{version}", it["diff"])

    def failed(self, m: Model, wid: str, it: dict, state: str):
        set_backoff(m, wid, it["key"], state)
