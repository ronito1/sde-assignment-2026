"""Variant experiments on welcome message and voice (C6), promotion, decay detection and rollback (C7).

Design (see SUBMISSION §5 for the reasoning and the false-winner arithmetic):
- Only for workflows whose primary KPI is engagement: welcome and voice are heard after pickup, so on a
  pickup-primary workflow they can only move the guardrail. There we leave them alone.
- The simulator samples welcome and voice independently per dial, so running both experiments at once is a
  2-factor factorial: every connected call is evidence for both, and each dimension is analysed on its
  margin (unbiased because the other factor is randomised independently at the same time).
- Launch: control keeps 70%, challengers share 30% equally. The cap bounds the damage of a bad challenger
  set before we can see it. Allocations then stay fixed between looks (no bandit): every change costs a
  governance request, and a fixed design keeps the test valid.
- Evidence = only calls whose recorded `variant` carries the arm, dialled after the experiment went live.
- Drop rule (hourly): an arm significantly worse than control (one-sided p < 0.05, >= 250 connects) loses its
  traffic to control. Cheap and safe, so it uses a loose alpha.
- Winner rule (one look per day): one-sided two-proportion test vs control, alpha 0.05 spread over 6 looks
  and Holm-corrected across arms; plus >= 2% relative lift, the arm still ahead of control over the last
  24h, and no significant guardrail breach. Deterministic: same data, same answer.
- Promotion keeps 15% on the old control as a holdout. Decay = champion significantly worse than holdout over
  the trailing 24h (alpha 0.10: rolling back to a proven control is cheap), or a significant guardrail breach.
- A supervisor edit to the experiment's dimension ends the experiment; their choice becomes the new control.
"""
from __future__ import annotations

import json

import numpy as np

from .. import policy, settings
from ..model import Model
from ..stats import holm, norm_sf, two_prop
from . import active_leads, backoff_ok, idx, inflight_keys, intent, set_backoff, stepped

DIMS = ("welcome_message", "voice")
COL = {"welcome_message": "wm", "voice": "vo"}


def _primary(wf):
    return wf["objective"]["primary"]


def _guard(wf):
    g = wf["objective"].get("guardrail") or {}
    if "pickup_rate_max_drop" in g:
        return "pickup", float(g["pickup_rate_max_drop"])
    if "engagement_rate_max_drop" in g:
        return "engagement", float(g["engagement_rate_max_drop"])
    return None, None


class ExperimentPlanner:
    kind = "experiment"

    # ------------------------------------------------------------------ state helpers
    def _exp(self, m, wid, dim):
        r = m.s.q1("SELECT eid, state, control, arms, start_minute, champion, promoted_minute, stats, ended_minute "
                   "FROM experiments WHERE wid=? AND dim=? ORDER BY rowid DESC LIMIT 1", (wid, dim))
        if not r:
            return None
        return {"eid": r[0], "state": r[1], "control": r[2], "arms": json.loads(r[3]), "start": r[4],
                "champion": r[5], "promoted": r[6], "stats": json.loads(r[7]) if r[7] else {}, "ended": r[8]}

    def _save(self, m, e, **kw):
        sets = ", ".join(f"{k}=?" for k in kw)
        vals = [json.dumps(v) if k in ("arms", "stats") else v for k, v in kw.items()]
        m.s.db.execute(f"UPDATE experiments SET {sets} WHERE eid=?", (*vals, e["eid"]))

    # ------------------------------------------------------------------ planning
    def plan(self, m: Model, wid: str, gov) -> list[dict]:
        wf = m.wf[wid]
        if m.now < 0 or _primary(wf) != "engagement_rate":
            return []
        self._gov = gov
        out = []
        keys = inflight_keys(m, wid)
        for dim in DIMS:
            if len(wf["options"][dim]) < 2:
                continue
            e = self._exp(m, wid, dim)
            if any(k.startswith(f"exp:{dim}:") for k in keys):
                continue
            if e is None or e["state"] in ("ENDED",):
                it = self._maybe_launch(m, wid, dim, e)
            elif e["state"] == "RUNNING":
                it = self._running(m, wid, dim, e)
            elif e["state"] == "PROMOTED":
                it = self._promoted(m, wid, dim, e)
            elif e["state"] == "ROLLING_BACK":
                it = self._continue_rollback(m, wid, dim, e)
            elif e["state"] == "DEMOTING":
                it = self._continue_demote(m, wid, dim, e)
            elif e["state"] == "DEMOTED":
                it = self._demoted(m, wid, dim, e)
            else:
                it = None
            if it:
                out.append(it)
        return out

    def _maybe_launch(self, m, wid, dim, prev):
        key = f"exp:{dim}:launch"
        if not backoff_ok(m, wid, key):
            return None
        if prev and prev["ended"] and m.now - prev["ended"] < settings.EXPERIMENT_COOLDOWN_MIN:
            return None
        wf = m.wf[wid]
        cur = policy.dist(m.cfg[wid]["config"], "default", dim)
        control = max(cur, key=lambda k: (cur[k], k))
        banned = set(m.s.get(f"exp_banned:{wid}:{dim}", []))
        tried_out = set(m.s.get(f"exp_lost:{wid}:{dim}", []))
        challengers = [o for o in wf["options"][dim] if o != control and o not in banned and o not in tried_out]
        if not challengers:
            return None
        share = settings.EXPLORE_SHARE / len(challengers)
        target = {control: round(1 - settings.EXPLORE_SHARE, 4), **{c: round(share, 4) for c in challengers}}
        conn_day = self._conn_per_day(m, wid)
        eta = settings.MIN_CONN_PER_ARM / max(conn_day * share, 1)
        return intent(key, self.kind, [("alloc", "default", dim, target)], [dim], "routine", auto_ok=False,
                      summary=f"Test {', '.join(challengers)} against {control} for {dim.replace('_', ' ')} "
                              f"on {settings.EXPLORE_SHARE:.0%} of calls",
                      diff={dim: {"from": cur, "to": target}},
                      rationale=(f"{control} has never been compared with the alternatives. Each challenger gets "
                                 f"{share:.0%} of calls; first decision possible in about {eta:.1f} days of calls. "
                                 f"Losers are dropped as soon as they are significantly worse."),
                      affected=int(active_leads(m, wid) * settings.EXPLORE_SHARE), impact=0.25,
                      dry_run={"method": "no variant history exists for challengers; worst case shown",
                               "connects_per_day": conn_day,
                               "exposure_share": settings.EXPLORE_SHARE,
                               "worst_case_engagement_change_if_challengers_10pct_worse":
                                   round(-0.10 * settings.EXPLORE_SHARE, 4)},
                      evidence={"control": control, "challengers": challengers},
                      meta={"dim": dim, "control": control, "arms": target, "action": "launch"})

    def _arm_stats(self, m, wid, dim, since, until=None):
        ix = idx(m, wid, since=since, until=until)
        C = m.calls
        v = C[COL[dim]][ix]
        out = {}
        for code in np.unique(v):
            sel = ix[v == code]
            out[m.s.name(code)] = {"dials": int(len(sel)), "conn": int(C["conn"][sel].sum()),
                                   "eng": int(C["eng"][sel].sum())}
        return out

    def _guard_ok(self, m, wid, a, b) -> tuple[bool, dict]:
        """Is arm a's guardrail KPI significantly worse than b's by more than the allowed drop?"""
        kpi, drop = _guard(m.wf[wid])
        if kpi is None:
            return True, {}
        if kpi == "pickup":
            t = two_prop(a["conn"], a["dials"], b["conn"], b["dials"])
        else:
            t = two_prop(a["eng"], a["conn"], b["eng"], b["conn"])
        # breach only with evidence: the drop beyond the allowance is itself significant
        se = t["se"] if np.isfinite(t["se"]) else 1.0
        z = (t["diff"] + drop) / se
        breach = z < settings.GUARDRAIL_Z if se > 0 else False
        return not breach, {"kpi": kpi, "allowed_drop": drop, "diff": round(t["diff"], 4), "z_vs_allowance": round(z, 2)}

    def _running(self, m, wid, dim, e):
        wf = m.wf[wid]
        st = self._arm_stats(m, wid, dim, e["start"])
        ctrl = st.get(e["control"], {"dials": 0, "conn": 0, "eng": 0})
        live = {a: s for a, s in e["arms"].items() if s > 0}
        res, p_better, drop = {}, {}, []
        for arm in live:
            if arm == e["control"]:
                continue
            a = st.get(arm, {"dials": 0, "conn": 0, "eng": 0})
            t = two_prop(a["eng"], a["conn"], ctrl["eng"], ctrl["conn"])
            gok, g = self._guard_ok(m, wid, a, ctrl)
            res[arm] = {"n": a["conn"], "rate": t["p1"], "control_rate": t["p2"], "rel_lift": round(t["rel"], 4),
                        "p_better": round(t["p_better"], 5), "p_worse": round(t["p_worse"], 5), "guardrail": g}
            if a["conn"] >= settings.MIN_CONN_PER_ARM and ctrl["conn"] >= settings.MIN_CONN_PER_ARM:
                if t["p_worse"] < settings.ALPHA_DROP or not gok:
                    drop.append(arm)
                else:
                    p_better[arm] = t["p_better"]
        e["stats"]["latest"] = {"minute": m.now, "control": ctrl, "arms": res}
        with m.s.tx():
            self._save(m, e, stats=e["stats"])
        cur = policy.dist(m.cfg[wid]["config"], "default", dim)
        auto = policy.can_auto(wf, [dim])
        if drop and not self._monitor_due(m, e):
            drop = []
        if drop:
            target = dict(live)
            for a in drop:
                target[e["control"]] = target.get(e["control"], 0) + target.pop(a)
            step = stepped(m, wid, "default", dim, target, auto)
            return intent(f"exp:{dim}:drop", self.kind, [("alloc", "default", dim, step)], [dim], "safety",
                          auto_ok=True,
                          summary=f"Stop {', '.join(drop)} for {dim.replace('_', ' ')}: significantly worse than "
                                  f"{e['control']}",
                          diff={dim: {"from": cur, "to": step}},
                          rationale="; ".join(f"{a}: {res[a]['rate']:.3f} vs {res[a]['control_rate']:.3f} "
                                              f"engaged/connected over {res[a]['n']} connects, p={res[a]['p_worse']}"
                                              for a in drop),
                          affected=active_leads(m, wid), impact=0.5,
                          dry_run={"method": "measured arm rates applied to last day's connects",
                                   **self._replay(m, wid, dim, cur, step, st)},
                          evidence={"arms": res, "control": ctrl},
                          meta={"dim": dim, "action": "drop", "drop": drop, "target": target})
        # winner: one look per day, only when a request could actually go out
        day = m.now // 1440
        if e["stats"].get("last_look_day") == day or not p_better:
            return None
        ok, _ = self._gov.can_submit(wid, "routine")
        if not ok:
            return None
        looks = e["stats"].get("looks", 0) + 1
        e["stats"]["looks"] = looks
        e["stats"]["last_look_day"] = day
        alpha = settings.ALPHA_WIN / settings.MAX_LOOKS
        winners = holm(p_better, alpha)
        winners = [a for a in winners if res[a]["rel_lift"] >= settings.MIN_REL_LIFT]
        recent = self._arm_stats(m, wid, dim, max(e["start"], m.now - 1440))
        rc = recent.get(e["control"], {"conn": 0, "eng": 0})
        winners = [a for a in winners if two_prop(recent.get(a, {}).get("eng", 0), recent.get(a, {}).get("conn", 0),
                                                 rc["eng"], rc["conn"])["diff"] >= 0]
        with m.s.tx():
            self._save(m, e, stats=e["stats"])
            m.s.decide(m.now, wid, "experiment_look",
                       f"{dim} look {looks}/{settings.MAX_LOOKS}: " +
                       (f"winner {max(winners, key=lambda a: res[a]['rel_lift'])}" if winners else "no winner yet"),
                       {"alpha_per_look": alpha, "arms": res, "control": ctrl, "eid": e["eid"]})
        if winners:
            best = max(winners, key=lambda a: res[a]["rel_lift"])
            target = {best: round(1 - settings.HOLDOUT_SHARE, 4), e["control"]: settings.HOLDOUT_SHARE}
            r = res[best]
            return intent(f"exp:{dim}:promote", self.kind, [("alloc", "default", dim, target)], [dim], "routine",
                          auto_ok=False,
                          summary=f"Make {best} the main {dim.replace('_', ' ')} ({r['rel_lift']:+.1%} engagement vs "
                                  f"{e['control']}), keep {settings.HOLDOUT_SHARE:.0%} on {e['control']} to watch for decay",
                          diff={dim: {"from": cur, "to": target}},
                          rationale=(f"{best}: {r['rate']:.3f} vs {r['control_rate']:.3f} engaged/connected over "
                                     f"{r['n']} connects, one-sided p={r['p_better']} (threshold {alpha / len(p_better):.4f} "
                                     f"after spreading alpha over {settings.MAX_LOOKS} looks and Holm over "
                                     f"{len(p_better)} arms); still ahead over the last 24h; guardrail {r['guardrail']}"),
                          affected=active_leads(m, wid),
                          impact=min(1.0, max(0.0, r["rel_lift"]) / 0.10) * (1 - r["p_better"]),
                          dry_run={"method": "measured arm rates applied to last day's connects",
                                   **self._replay(m, wid, dim, cur, target, st)},
                          evidence={"arms": res, "control": ctrl, "eid": e["eid"]},
                          meta={"dim": dim, "action": "promote", "champion": best, "target": target})
        if looks >= settings.MAX_LOOKS:
            target = {e["control"]: 1.0}
            return intent(f"exp:{dim}:stop", self.kind, [("alloc", "default", dim, stepped(m, wid, "default", dim,
                                                                                          target, auto))], [dim],
                          "safety", auto_ok=True,
                          summary=f"End the {dim.replace('_', ' ')} test: no challenger beat {e['control']}",
                          diff={dim: {"from": cur, "to": target}},
                          rationale=f"{looks} looks without a significant winner; alpha budget spent.",
                          affected=active_leads(m, wid), impact=0.1,
                          dry_run={"method": "measured arm rates", **self._replay(m, wid, dim, cur, target, st)},
                          evidence={"arms": res}, meta={"dim": dim, "action": "stop", "target": target})
        return None

    def _promoted(self, m, wid, dim, e):
        champ, hold = e["champion"], e["control"]
        since = max(e["promoted"], m.now - settings.DECAY_WINDOW_MIN)
        st = self._arm_stats(m, wid, dim, since)
        c = st.get(champ, {"dials": 0, "conn": 0, "eng": 0})
        h = st.get(hold, {"dials": 0, "conn": 0, "eng": 0})
        t = two_prop(c["eng"], c["conn"], h["eng"], h["conn"])
        gok, g = self._guard_ok(m, wid, c, h)
        e["stats"]["watch"] = {"minute": m.now, "champion": c, "holdout": h, "p_worse": round(t["p_worse"], 4),
                               "rel": round(t["rel"], 4), "guardrail": g}
        with m.s.tx():
            self._save(m, e, stats=e["stats"])
        # Signal B (power): the champion's own engagement over the last 24h vs what it showed in the experiment.
        # The holdout is too small to see a gradual decay quickly, but the champion carries most of the traffic.
        # Vetoed if the holdout fell as much (a base shift, e.g. a prompt change), and only acted on once the
        # champion is no better than the holdout over the same 24h. The first step is a 50/50 demotion.
        drift = None
        if m.now - settings.DECAY_WINDOW_MIN >= e["promoted"] + 360:
            # reference = the experiment that crowned it (before promotion): the champion as an arm, and the
            # control at its full experimental share (precise). The champion's reference uses the lower 80% bound
            # of its rate, so winner's curse does not look like decay.
            ref = self._arm_stats(m, wid, dim, e["start"], e["promoted"])
            c0 = ref.get(champ, {"conn": 0, "eng": 0})
            h0 = ref.get(hold, {"conn": 0, "eng": 0})
            if c0["conn"] >= 100 and h0["conn"] >= 100:
                p0 = c0["eng"] / c0["conn"]
                lb = p0 - 0.8416 * (p0 * (1 - p0) / c0["conn"]) ** 0.5
                ph = c["eng"] / c["conn"] if c["conn"] else lb
                se = ((ph * (1 - ph) / max(c["conn"], 1)) + (p0 * (1 - p0) / c0["conn"])) ** 0.5
                z = (ph - lb) / se if se > 0 else 0.0
                dh = two_prop(h["eng"], h["conn"], h0["eng"], h0["conn"])
                drift = {"champion_now": round(ph, 4), "champion_ref_lb80": round(lb, 4), "z": round(z, 2),
                         "holdout_change": round(dh["diff"], 4), "champion_vs_holdout_24h": round(t["diff"], 4)}
                e["stats"]["watch"]["drift"] = drift
                with m.s.tx():
                    self._save(m, e, stats=e["stats"])
                if norm_sf(-z) < settings.DECAY_DRIFT_ALPHA and dh["diff"] > (ph - lb) / 2 and t["diff"] <= 0 \
                        and self._monitor_due(m, e):
                    return self._decay(m, wid, dim, e, champ, hold, since, t, "drift")
        if h["conn"] < settings.DECAY_MIN_CONN:
            return None
        if (t["p_worse"] < settings.DECAY_ALPHA or not gok) and self._monitor_due(m, e):
            return self._decay(m, wid, dim, e, champ, hold, since, t, "vs_holdout" if gok else "guardrail")
        return None

    def _monitor_due(self, m, e) -> bool:
        """At most one monitoring decision per 6h per experiment (bounds the repeated-looks false-alarm rate)."""
        last = e["stats"].get("last_monitor", -10 ** 9)
        if m.now - last < settings.MONITOR_EVERY_MIN:
            return False
        e["stats"]["last_monitor"] = m.now
        with m.s.tx():
            self._save(m, e, stats=e["stats"])
        return True

    def _decay(self, m, wid, dim, e, champ, hold, since, t, why):
        """First response to decay: split 50/50 between champion and holdout. Half the exposure if it really
        decayed, half the regret if it did not, and a 50/50 split gives the follow-up test real power."""
        with m.s.tx():
            m.s.decide(m.now, wid, "decay_suspected",
                       f"{dim} {champ} may have stopped winning ({why}): {t['rel']:+.1%} vs holdout {hold} over the "
                       f"last {(m.now - since) / 60:.0f}h (p={t['p_worse']:.3f}); demoting to 50/50 to confirm",
                       e["stats"]["watch"])
            self._save(m, e, state="DEMOTING")
        e["state"] = "DEMOTING"
        return self._continue_demote(m, wid, dim, e)

    def _continue_demote(self, m, wid, dim, e):
        target = {e["champion"]: 0.5, e["control"]: 0.5}
        cur = policy.dist(m.cfg[wid]["config"], "default", dim)
        if all(abs(cur.get(k, 0) - v) < 0.01 for k, v in target.items()):
            with m.s.tx():
                e["stats"]["demoted_minute"] = m.now
                self._save(m, e, state="DEMOTED", stats=e["stats"])
            return None
        auto = policy.can_auto(m.wf[wid], [dim])
        step = stepped(m, wid, "default", dim, target, auto)
        w = e["stats"].get("watch", {})
        return intent(f"exp:{dim}:demote", self.kind, [("alloc", "default", dim, step)], [dim], "safety",
                      auto_ok=True,
                      summary=f"Halve {dim.replace('_', ' ')} {e['champion']} to 50% (back to {e['control']} for the "
                              f"other half): its lead is fading",
                      diff={dim: {"from": cur, "to": step}},
                      rationale=(f"{e['champion']} engagement has fallen since promotion (drift {w.get('drift')}); "
                                 f"vs holdout {e['control']} over the trailing window {w.get('rel', 0):+.1%}. A 50/50 "
                                 f"split confirms or clears this within about a day."),
                      affected=active_leads(m, wid), impact=0.6,
                      dry_run={"method": "trailing-window champion vs holdout", **w},
                      evidence=w, meta={"dim": dim, "action": "demote", "target": target})

    def _demoted(self, m, wid, dim, e):
        since = e["stats"].get("demoted_minute", m.now)
        st = self._arm_stats(m, wid, dim, since)
        c = st.get(e["champion"], {"dials": 0, "conn": 0, "eng": 0})
        h = st.get(e["control"], {"dials": 0, "conn": 0, "eng": 0})
        t = two_prop(c["eng"], c["conn"], h["eng"], h["conn"])
        e["stats"]["watch"] = {"minute": m.now, "champion": c, "holdout": h, "rel": round(t["rel"], 4),
                               "p_worse": round(t["p_worse"], 4), "p_better": round(t["p_better"], 4), "phase": "50/50"}
        with m.s.tx():
            self._save(m, e, stats=e["stats"])
        if min(c["conn"], h["conn"]) < settings.DECAY_MIN_CONN:
            return None
        if t["diff"] <= 0:
            with m.s.tx():
                m.s.decide(m.now, wid, "decay_detected",
                           f"{dim} {e['champion']} confirmed no better than {e['control']} at 50/50 "
                           f"({t['rel']:+.1%} over {c['conn']}+{h['conn']} connects); rolling back", e["stats"]["watch"])
                self._save(m, e, state="ROLLING_BACK")
                banned = set(m.s.get(f"exp_banned:{wid}:{dim}", [])) | {e["champion"]}
                m.s.put(f"exp_banned:{wid}:{dim}", sorted(banned))
            e["state"] = "ROLLING_BACK"
            return self._continue_rollback(m, wid, dim, e)
        if t["p_better"] < 0.01 and m.now - since >= 1440:
            with m.s.tx():
                m.s.decide(m.now, wid, "decay_cleared", f"{dim} {e['champion']} still clearly ahead at 50/50 "
                           f"({t['rel']:+.1%}); re-promoting", e["stats"]["watch"])
            target = {e["champion"]: round(1 - settings.HOLDOUT_SHARE, 4), e["control"]: settings.HOLDOUT_SHARE}
            cur = policy.dist(m.cfg[wid]["config"], "default", dim)
            return intent(f"exp:{dim}:promote", self.kind, [("alloc", "default", dim, target)], [dim], "routine",
                          auto_ok=False, summary=f"Restore {e['champion']} as main {dim.replace('_', ' ')}: the dip was noise",
                          diff={dim: {"from": cur, "to": target}},
                          rationale=f"At 50/50 {e['champion']} beats {e['control']} by {t['rel']:+.1%} (p={t['p_better']:.4f})",
                          affected=active_leads(m, wid), impact=min(1.0, max(0.0, t["rel"]) / 0.10),
                          dry_run={"method": "50/50 champion vs holdout", **e["stats"]["watch"]},
                          evidence=e["stats"]["watch"],
                          meta={"dim": dim, "action": "promote", "champion": e["champion"], "target": target})
        return None

    def _continue_rollback(self, m, wid, dim, e):
        wf = m.wf[wid]
        target = {e["control"]: 1.0}
        cur = policy.dist(m.cfg[wid]["config"], "default", dim)
        if cur == target or (len(cur) == 1 and e["control"] in cur):
            with m.s.tx():
                self._save(m, e, state="ENDED", ended_minute=m.now, note="rolled back")
            return None
        auto = policy.can_auto(wf, [dim])
        step = stepped(m, wid, "default", dim, target, auto)
        w = e["stats"].get("watch", {})
        return intent(f"exp:{dim}:rollback", self.kind, [("alloc", "default", dim, step)], [dim], "safety",
                      auto_ok=True,
                      summary=f"Roll back {dim.replace('_', ' ')} {e['champion']} → {e['control']}: it stopped winning",
                      diff={dim: {"from": cur, "to": step}},
                      rationale=(f"Champion {e['champion']} vs holdout {e['control']} over the trailing window: "
                                 f"{w.get('rel', 0):+.1%}, p={w.get('p_worse')}; guardrail {w.get('guardrail')}"),
                      affected=active_leads(m, wid), impact=0.8,
                      dry_run={"method": "trailing-window champion vs holdout", **w},
                      evidence=w, meta={"dim": dim, "action": "rollback", "target": target})

    def _replay(self, m, wid, dim, cur, new, st):
        rates = {a: (s["eng"] / s["conn"]) if s["conn"] else None for a, s in st.items()}
        known = [r for r in rates.values() if r is not None]
        fallback = float(np.mean(known)) if known else None
        if fallback is None:
            return {"note": "no data"}
        f = lambda d: sum(p * (rates.get(a) if rates.get(a) is not None else fallback) for a, p in d.items())
        conn_day = self._conn_per_day(m, wid)
        return {"engagement_before": round(f(cur), 4), "engagement_after": round(f(new), 4),
                "extra_engaged_per_day": round((f(new) - f(cur)) * conn_day, 1), "connects_per_day": conn_day}

    def _conn_per_day(self, m, wid):
        ix = idx(m, wid, since=m.now - 2 * 1440, hist=None)
        return int(m.calls["conn"][ix].sum() / 2)

    # ------------------------------------------------------------------ hooks
    def applied(self, m: Model, wid: str, it: dict, version: int):
        meta = it["meta"]
        dim = meta["dim"]
        e = self._exp(m, wid, dim)
        act = meta["action"]
        if act == "launch":
            eid = f"{wid}:{dim}:{m.now}"
            m.s.db.execute("INSERT INTO experiments(eid, wid, dim, cohort, state, control, arms, start_minute, stats) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (eid, wid, dim, "default", "RUNNING", meta["control"],
                                                         json.dumps(meta["arms"]), m.now, json.dumps({})))
            m.s.decide(m.now, wid, "experiment_started", f"{dim}: {meta['arms']} live in v{version}", meta)
        elif e is None:
            return
        elif act == "drop":
            cur = {a: v for a, v in policy.dist(m.cfg[wid]["config"], "default", dim).items()}
            arms = dict(e["arms"])
            if it["ops"][0][3] == meta["target"]:
                arms = meta["target"]
            lost = set(m.s.get(f"exp_lost:{wid}:{dim}", [])) | set(meta["drop"])
            m.s.put(f"exp_lost:{wid}:{dim}", sorted(lost))
            self._save(m, e, arms=arms)
            live = [a for a, s in arms.items() if s > 0 and a != e["control"]]
            if not live:
                self._save(m, e, state="ENDED", ended_minute=m.now, note="all challengers lost")
            m.s.decide(m.now, wid, "experiment_arm_dropped", f"{dim}: dropped {meta['drop']} (v{version})", cur)
        elif act == "promote":
            self._save(m, e, state="PROMOTED", champion=meta["champion"], promoted_minute=m.now)
            others = [a for a in e["arms"] if a not in (meta["champion"], e["control"])]
            m.s.decide(m.now, wid, "experiment_promoted", f"{dim}: {meta['champion']} promoted in v{version}", meta)
        elif act == "stop":
            if it["ops"][0][3] == meta["target"]:
                self._save(m, e, state="ENDED", ended_minute=m.now, note="no winner")
        elif act == "rollback":
            m.s.decide(m.now, wid, "rollback_step", f"{dim}: rollback step live in v{version}", it["diff"])
        elif act == "demote":
            m.s.decide(m.now, wid, "demote_step", f"{dim}: 50/50 demotion step live in v{version}", it["diff"])

    def failed(self, m: Model, wid: str, it: dict, state: str):
        set_backoff(m, wid, it["key"], state)

    def supervisor_edit(self, m: Model, wid: str, dims: list[str], version: int):
        for dim in DIMS:
            if dim not in dims:
                continue
            e = self._exp(m, wid, dim)
            if e and e["state"] in ("RUNNING", "PROMOTED", "ROLLING_BACK", "DEMOTING", "DEMOTED"):
                self._save(m, e, state="ENDED", ended_minute=m.now, note=f"supervisor edit v{version}")
                m.s.decide(m.now, wid, "experiment_ended_by_supervisor",
                           f"{dim} experiment {e['eid']} ended: supervisor changed {dim} in v{version}; their "
                           f"choice is the new control and is not reverted", {"eid": e["eid"]})
