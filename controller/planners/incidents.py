"""Hallucination incident response (C8): detect → pick a fix → validate → propose with evidence → verify.

Detection is population-based: the share of connected calls with `hallucination_flag` (an imperfect
detector, so we compare against the workflow's own pre-D0 flag rate, which already contains the detector's
false-positive rate). It fires when, over >= 300 connects under the current prompt (since the prompt last
changed, or over the last 800 connects), the exact binomial tail p < 0.001 AND the rate is >= 1.8x baseline.

Fix candidates, cheapest first (the best compute is no compute):
  1. the prompt that ran before the suspect change, if its own live flag rate was normal (it also has live
     engagement evidence, which validation cannot give us);
  2. other prompts we have live evidence for, best flag rate first;
  3. only then POST /llm/suggest-fixes (3 credits).
Each candidate is validated (n=300, 3 credits) before it can be proposed; the approver requires it anyway.
Verification after apply: >= 300 connects on the new prompt with a flag rate not significantly above
baseline and engagement not significantly below the pre-incident level; otherwise the incident reopens with
the next candidate.
"""
from __future__ import annotations

import json

import numpy as np

from .. import settings
from ..client import SimError
from ..model import Model
from ..stats import binom_sf, two_prop
from . import active_leads, idx, inflight_keys, intent

KEY = "incident:prompt"


class IncidentPlanner:
    kind = "incident"

    def _open(self, m, wid):
        r = m.s.q1("SELECT iid, state, opened_minute, since_minute, suspect, evidence, fix FROM incidents WHERE wid=? "
                   "AND kind='hallucination' AND state != 'RESOLVED' ORDER BY rowid DESC LIMIT 1", (wid,))
        if not r:
            return None
        return {"iid": r[0], "state": r[1], "opened": r[2], "since": r[3], "suspect": r[4],
                "evidence": json.loads(r[5] or "{}"), "fix": json.loads(r[6] or "{}")}

    def baseline(self, m, wid) -> float:
        b = m.s.get(f"halluc_baseline:{wid}")
        if b is None:
            ix = idx(m, wid, hist=True)
            C = m.calls
            conn = C["conn"][ix]
            n = int(conn.sum())
            k = int(C["hf"][ix][conn].sum())
            b = (k + 1) / (n + 50) if n else 0.015
            with m.s.tx():
                m.s.put(f"halluc_baseline:{wid}", b)
        return b

    def prompt_since(self, m, wid) -> int:
        """Minute the current prompt went live (start of evidence for it)."""
        cur = m.cfg[wid]["config"]["prompt_version"]
        rows = m.s.q("SELECT version, minute, changed_dims FROM config_versions WHERE wid=? ORDER BY version DESC",
                     (wid,))
        for v, minute, dims in rows:
            if "prompt_version" in json.loads(dims or "[]"):
                return minute
        return -10 ** 9

    def rate(self, m, wid, pv: str, since: int | None = None, last_n: int | None = None):
        C = m.calls
        ix = idx(m, wid, since=since, hist=None)
        ix = ix[C["conn"][ix] & (C["pv"][ix] == m.s.code(pv))]
        if last_n:
            ix = ix[-last_n:]
        return int(C["hf"][ix].sum()), int(len(ix)), ix

    def plan(self, m: Model, wid: str, gov) -> list[dict]:
        if m.now < 0:
            return []
        inc = self._open(m, wid)
        if inc is None and m.s.get(f"check_prompt:{wid}") is not None:
            return self._check_new_prompt(m, wid)
        if inc is None:
            return self._detect(m, wid)
        if inc["state"] == "VERIFYING":
            return self._verify(m, wid, inc)
        if inc["state"] == "OPEN":
            if KEY in inflight_keys(m, wid):
                return []
            return self._fix(m, wid, inc)
        return []

    def _detect(self, m, wid):
        b = self.baseline(m, wid)
        pv = m.cfg[wid]["config"]["prompt_version"]
        since = self.prompt_since(m, wid)
        if since > -10 ** 9:
            k, n, _ = self.rate(m, wid, pv, since=since)
        else:
            k, n, _ = self.rate(m, wid, pv, since=0, last_n=settings.HALLUC_WINDOW_CONN)
        if n < settings.HALLUC_MIN_CONN:
            return []
        p = binom_sf(k, n, b)
        # Without a prompt change to explain it, the rolling window is re-tested every iteration on every
        # workflow (thousands of looks per scenario), so it needs a far smaller p to keep false alarms rare.
        p_need = settings.HALLUC_P if since > -10 ** 9 else settings.HALLUC_P_ROLLING
        if p < p_need and k / n >= settings.HALLUC_MIN_RATIO * b:
            ev = {"prompt": pv, "flags": k, "connects": n, "rate": round(k / n, 4), "baseline": round(b, 4),
                  "p_value": p, "since": m.fmt(since) if since > -10 ** 9 else "rolling window"}
            iid = f"{wid}:halluc:{m.now}"
            with m.s.tx():
                m.s.db.execute("INSERT INTO incidents(iid, wid, kind, state, opened_minute, since_minute, suspect, "
                               "evidence, fix) VALUES(?,?,?,?,?,?,?,?,?)",
                               (iid, wid, "hallucination", "OPEN", m.now, since, pv, json.dumps(ev),
                                json.dumps({"tried": [], "validations": {}})))
                m.s.decide(m.now, wid, "incident_opened",
                           f"hallucination flags {k}/{n}={k / n:.1%} on {pv} vs baseline {b:.1%} (p={p:.1e})", ev)
                m.s.alert(m.now, "page", wid, "hallucination_incident",
                          f"{wid}: hallucination flag rate {k / n:.1%} on prompt {pv} (baseline {b:.1%})",
                          f"python -m controller why --workflow {wid} --incident; fix is being validated")
            return self._fix(m, wid, self._open(m, wid))
        return []

    def _candidates(self, m, wid, inc) -> list[str]:
        opts = m.wf[wid]["options"]["prompt_version"]
        b = self.baseline(m, wid)
        tried = set(inc["fix"].get("tried", []))
        good, unknown = [], []
        prev = self._previous_prompt(m, wid, inc["suspect"])
        for pv in opts:
            if pv == inc["suspect"] or pv in tried:
                continue
            k, n, _ = self.rate(m, wid, pv)
            kh, nh = self._hist_rate(m, wid, pv)
            k, n = k + kh, n + nh
            if n >= 200:
                p = binom_sf(k, n, b)
                if p > 0.01:
                    good.append((0 if pv == prev else 1, k / n, pv))
            else:
                unknown.append(pv)
        return [pv for _, _, pv in sorted(good)] + [pv for pv in unknown if pv in inc["fix"].get("llm", [])]

    def _hist_rate(self, m, wid, pv):
        C = m.calls
        ix = idx(m, wid, hist=True)
        ix = ix[C["conn"][ix] & (C["pv"][ix] == m.s.code(pv))]
        return int(C["hf"][ix].sum()), int(len(ix))

    def _previous_prompt(self, m, wid, suspect):
        rows = m.s.q("SELECT config FROM config_versions WHERE wid=? AND config IS NOT NULL ORDER BY version DESC",
                     (wid,))
        for (cfg,) in rows:
            pv = json.loads(cfg).get("prompt_version")
            if pv and pv != suspect:
                return pv
        return None

    def _credits(self, m):
        return m.c.state()["credits_remaining"]

    def _fix(self, m, wid, inc):
        fix = inc["fix"]
        cands = self._candidates(m, wid, inc)
        if not cands and not fix.get("llm_asked"):
            if self._credits(m) >= settings.LLM_COST:
                try:
                    r = m.c.suggest_fixes(wid, f"hallucination flag rate {inc['evidence'].get('rate')} on prompt "
                                               f"{inc['suspect']} vs baseline {inc['evidence'].get('baseline')}")
                except SimError as e:
                    r = {"suggestions": [], "error": e.code}
                with m.s.tx():
                    fix["llm_asked"] = True
                    fix["llm"] = [s["prompt_version"] for s in r.get("suggestions", [])]
                    fix["llm_summaries"] = r.get("suggestions", [])
                    m.s.db.execute("INSERT INTO credits(minute, wid, kind, cost, ref, result) VALUES(?,?,?,?,?,?)",
                                   (m.now, wid, "llm_suggest_fixes", settings.LLM_COST, inc["iid"], json.dumps(r)))
                    m.s.db.execute("UPDATE incidents SET fix=? WHERE iid=?", (json.dumps(fix), inc["iid"]))
                    m.s.decide(m.now, wid, "llm_suggested", f"LLM suggested {fix['llm']}", r)
                inc["fix"] = fix
                cands = self._candidates(m, wid, inc)
        for pv in cands:
            val = fix["validations"].get(pv)
            if val is None:
                if self._credits(m) < (settings.VALIDATION_N + 99) // 100:
                    with m.s.tx():
                        m.s.alert(m.now, "page", wid, "credits_exhausted", "no credits left to validate a prompt fix",
                                  "credit ledger: python -m controller status")
                    return []
                try:
                    val = m.c.validate(wid, pv, settings.VALIDATION_N)
                except SimError as e:
                    val = {"error": e.code, "pass_rate": 0}
                with m.s.tx():
                    fix["validations"][pv] = val
                    m.s.db.execute("INSERT INTO credits(minute, wid, kind, cost, ref, result) VALUES(?,?,?,?,?,?)",
                                   (m.now, wid, "validation_run", (settings.VALIDATION_N + 99) // 100, inc["iid"],
                                    json.dumps(val)))
                    m.s.db.execute("UPDATE incidents SET fix=? WHERE iid=?", (json.dumps(fix), inc["iid"]))
                    m.s.decide(m.now, wid, "validation_run", f"{pv}: pass rate {val.get('pass_rate')}", val)
            need = m.wf[wid]["governance"].get("min_validation_pass_rate", 0.97)
            if val.get("pass_rate", 0) >= need and val.get("validation_run_id"):
                return [self._propose(m, wid, inc, pv, val)]
            with m.s.tx():
                fix["tried"] = sorted(set(fix.get("tried", [])) | {pv})
                m.s.db.execute("UPDATE incidents SET fix=? WHERE iid=?", (json.dumps(fix), inc["iid"]))
        return []

    def _propose(self, m, wid, inc, pv, val):
        ev = inc["evidence"]
        k, n, _ = self.rate(m, wid, pv)
        kh, nh = self._hist_rate(m, wid, pv)
        live = {"flags": k + kh, "connects": n + nh}
        return intent(KEY, self.kind, [("prompt", pv)], ["prompt_version"], "incident", auto_ok=False,
                      summary=f"Fix hallucinations: switch prompt {inc['suspect']} → {pv}",
                      diff={"prompt_version": {"from": inc["suspect"], "to": pv}},
                      rationale=(self._evidence_text(inc) + f" {pv} passed validation at "
                                 f"{val['pass_rate']:.3f} on {val['n']} conversations and has a normal live flag "
                                 f"record ({live['flags']}/{live['connects']})."
                                 + (" This reverses a supervisor's prompt edit; it is proposed, never forced."
                                    if self._supervisor_made(m, wid, inc) else "")),
                      affected=active_leads(m, wid), impact=0.95,
                      dry_run={"method": "validation run + live flag history of the candidate prompt",
                               "validation": val, "candidate_live_flags": live, "incident": ev},
                      evidence={"incident": inc["iid"], "validation": val},
                      validation_run_id=val["validation_run_id"],
                      meta={"iid": inc["iid"], "prompt": pv})

    @staticmethod
    def _evidence_text(inc) -> str:
        ev = inc["evidence"]
        if ev.get("validation") and ev.get("p_value") is None:
            v = ev["validation"]
            return (f"{inc['suspect']} (live since {ev.get('since')}) failed validation: pass rate {v['pass_rate']} on "
                    f"{v['n']} synthetic conversations ({v['validation_run_id']}).")
        return (f"Since {ev.get('since')}, {ev.get('flags')}/{ev.get('connects')} connected calls on {inc['suspect']} "
                f"were flagged ({ev.get('rate'):.1%}) vs a {ev.get('baseline'):.1%} baseline "
                f"(p={ev.get('p_value'):.1e}).")

    def _supervisor_made(self, m, wid, inc) -> bool:
        r = m.s.q1("SELECT actor FROM config_versions WHERE wid=? AND minute=? ORDER BY version DESC LIMIT 1",
                   (wid, inc["since"]))
        return bool(r and r[0] == "supervisor")

    def _verify(self, m, wid, inc):
        fix = inc["fix"]
        pv = fix.get("applied_prompt")
        if m.cfg[wid]["config"]["prompt_version"] != pv:
            with m.s.tx():
                m.s.db.execute("UPDATE incidents SET state='OPEN' WHERE iid=?", (inc["iid"],))
                m.s.decide(m.now, wid, "incident_reopened", "prompt changed again before verification", {})
            return []
        k, n, ix = self.rate(m, wid, pv, since=fix["applied_minute"])
        if n < settings.HALLUC_MIN_CONN:
            return []
        b = self.baseline(m, wid)
        p = binom_sf(k, n, b)
        C = m.calls
        pre = idx(m, wid, since=inc["since"] - 1440, until=inc["since"])
        pre = pre[C["conn"][pre]]
        eng = two_prop(int(C["eng"][ix].sum()), n, int(C["eng"][pre].sum()), len(pre))
        ok_h = p > 0.01
        ok_e = not (eng["p_worse"] < 0.01 and eng["rel"] < -0.05)
        res = {"flags": k, "connects": n, "rate": round(k / n, 4), "baseline": round(b, 4), "p": p,
               "engagement_vs_pre": round(eng["rel"], 4), "engagement_p_worse": round(eng["p_worse"], 4)}
        with m.s.tx():
            if ok_h and ok_e:
                m.s.db.execute("UPDATE incidents SET state='RESOLVED', closed_minute=? WHERE iid=?",
                               (m.now, inc["iid"]))
                m.s.decide(m.now, wid, "incident_resolved",
                           f"{pv} verified: flags {k}/{n}={k / n:.1%}, engagement {eng['rel']:+.1%} vs pre-incident", res)
            else:
                fix["tried"] = sorted(set(fix.get("tried", [])) | {pv})
                m.s.db.execute("UPDATE incidents SET state='OPEN', suspect=?, fix=?, since_minute=? WHERE iid=?",
                               (pv, json.dumps(fix), fix["applied_minute"], inc["iid"]))
                m.s.decide(m.now, wid, "incident_fix_failed", f"{pv} did not verify", res)
                m.s.alert(m.now, "page", wid, "incident_fix_failed", f"{wid}: fix {pv} did not verify ({res})",
                          "incidents table; next candidate will be validated")
        return []

    def applied(self, m: Model, wid: str, it: dict, version: int):
        iid = it["meta"]["iid"]
        r = m.s.q1("SELECT fix FROM incidents WHERE iid=?", (iid,))
        fix = json.loads(r[0]) if r else {}
        fix.update({"applied_prompt": it["meta"]["prompt"], "applied_minute": m.now, "applied_version": version})
        m.s.db.execute("UPDATE incidents SET state='VERIFYING', fix=? WHERE iid=?", (json.dumps(fix), iid))
        m.s.decide(m.now, wid, "incident_fix_applied", f"{it['meta']['prompt']} live in v{version}; verifying", fix)

    def failed(self, m: Model, wid: str, it: dict, state: str):
        m.s.decide(m.now, wid, "incident_fix_not_applied", f"fix proposal {state}; will re-propose", {"state": state})

    def supervisor_edit(self, m: Model, wid: str, dims: list[str], version: int):
        if "prompt_version" in dims:
            m.s.put(f"check_prompt:{wid}", version)

    def _check_new_prompt(self, m, wid):
        """A supervisor shipped a prompt. If it has no clean live record, one validation run (3 credits) tells us
        within the hour whether it is unsafe, instead of waiting hours for live hallucination flags. This is
        the credit spend that changes a decision (CTO memo)."""
        pv = m.cfg[wid]["config"]["prompt_version"]
        with m.s.tx():
            m.s.put(f"check_prompt:{wid}", None)
        k, n, _ = self.rate(m, wid, pv)
        kh, nh = self._hist_rate(m, wid, pv)
        if n + nh >= 1000 and binom_sf(k + kh, n + nh, self.baseline(m, wid)) > 0.01:
            return []
        cost = (settings.VALIDATION_N + 99) // 100
        if self._credits(m) < cost:
            return []
        try:
            val = m.c.validate(wid, pv, settings.VALIDATION_N)
        except SimError:
            return []
        need = m.wf[wid]["governance"].get("min_validation_pass_rate", 0.97)
        since = self.prompt_since(m, wid)
        with m.s.tx():
            m.s.db.execute("INSERT INTO credits(minute, wid, kind, cost, ref, result) VALUES(?,?,?,?,?,?)",
                           (m.now, wid, "validation_run", cost, f"supervisor prompt {pv}", json.dumps(val)))
            m.s.decide(m.now, wid, "validation_run", f"supervisor's new prompt {pv}: pass rate {val['pass_rate']} "
                       f"(approval needs {need})", val)
            if val["pass_rate"] >= need:
                return []
            ev = {"prompt": pv, "source": "validation of a newly shipped prompt", "validation": val,
                  "flags": k, "connects": n, "rate": round(1 - val["pass_rate"], 4),
                  "baseline": round(self.baseline(m, wid), 4), "p_value": None, "since": m.fmt(since)}
            iid = f"{wid}:halluc:{m.now}"
            m.s.db.execute("INSERT INTO incidents(iid, wid, kind, state, opened_minute, since_minute, suspect, "
                           "evidence, fix) VALUES(?,?,?,?,?,?,?,?,?)",
                           (iid, wid, "hallucination", "OPEN", m.now, since, pv, json.dumps(ev),
                            json.dumps({"tried": [], "validations": {pv: val}})))
            m.s.decide(m.now, wid, "incident_opened", f"supervisor's prompt {pv} failed validation "
                       f"({val['pass_rate']} < {need} on {val['n']} conversations)", ev)
            m.s.alert(m.now, "page", wid, "hallucination_incident",
                      f"{wid}: newly shipped prompt {pv} failed validation ({val['pass_rate']})",
                      f"python -m controller why --workflow {wid}; a fix is being proposed")
        return self._fix(m, wid, self._open(m, wid))
