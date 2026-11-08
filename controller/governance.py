"""Governance client (C9): every config change the controller makes goes through here.

Proposal state machine (proposals.state):

    approval route:  PREPARED ─submit→ PENDING ─→ APPROVED ─apply→ APPLIED
                                        │            ├─(apply 409 base moved)→ STALE   → re-plan, new id
                                        │            └─(apply window passed)→  LAPSED  → re-plan, new id
                                        ├─→ REJECTED  (reasons recorded; planner backs off)
                                        └─→ EXPIRED   (treated as a rejection: re-plan from fresh data)
                     PREPARED ─submit 409 stale / 422→ STALE | INVALID
    auto route:      PREPARED ─PUT→ APPLIED | REFUSED (403) | STALE (409)

The outbox rule: a row is committed as PREPARED (with its proposal_id and full body) BEFORE any HTTP call.
After a crash, PREPARED rows are reconciled against the server by proposal_id, so nothing is sent twice and
nothing that was sent is forgotten.

Intents are what planners want; they are small, serialisable lists of config operations applied on top of
the *current* config at compose time, so a proposal is always built on the freshest base.
"""
from __future__ import annotations

import copy
import json
import logging

from . import cohorts as cohorts_mod
from . import policy, settings
from .client import SimError
from .model import Model
from .store import dumps

log = logging.getLogger("controller.governance")
INFLIGHT = ("PENDING", "APPROVED")
TERMINAL_FAIL = ("REJECTED", "EXPIRED", "STALE", "LAPSED", "REFUSED", "INVALID")


# ---------------------------------------------------------------------------- intents
def compose(cfg: dict, intents: list[dict]) -> dict:
    out = copy.deepcopy(cfg)
    out.setdefault("cohorts", [])
    out.setdefault("schedule", {"hour_weights": {}, "lead_preferred_hour": {}})
    out["schedule"].setdefault("hour_weights", {})
    out["schedule"].setdefault("lead_preferred_hour", {})
    ops = [op for it in intents for op in it["ops"]]
    ops.sort(key=lambda op: 0 if op[0] == "cohorts" else 1)   # cohorts must exist before they are referenced
    for op in ops:
        kind = op[0]
        if kind == "cohorts":
            out["cohorts"] = cohorts_mod.merged_cohorts(out, op[1])
        elif kind == "alloc":
            _, cohort, dim, d = op
            out["allocations"].setdefault(cohort, {})[dim] = d
        elif kind == "prompt":
            out["prompt_version"] = op[1]
        elif kind == "hour_weights":
            _, cohort, table = op
            out["schedule"]["hour_weights"][cohort] = table
    return out


class Governance:
    def __init__(self, m: Model, planners: dict):
        self.m, self.s, self.c = m, m.s, m.c
        self.planners = planners   # kind -> planner (applied/failed hooks)

    # ------------------------------------------------------------------ budget & timing
    def _limits(self, wid: str) -> dict:
        wf = self.m.wf[wid]
        gov = wf["governance"]
        ov = settings.TENANT_OVERRIDES.get(wf["tenant_id"], {})
        total = min(gov["max_requests_per_24h"], ov.get("max_requests_per_24h", 10 ** 6))
        return {"total": total, "routine": max(total - settings.INCIDENT_SLOTS, 1),
                "earliest": max(gov["approver_hours"][0], ov.get("earliest_submit_hour", 0)),
                "hours": gov["approver_hours"], "latency": gov["approver_latency_minutes"],
                "ttl_h": gov["proposal_ttl_hours"]}

    def used_24h(self, wid: str) -> int:
        r = self.s.q1("SELECT COUNT(*) FROM proposals WHERE wid=? AND route='approval' AND submitted_minute IS NOT "
                      "NULL AND submitted_minute > ?", (wid, self.m.now - 1440))
        return r[0]

    def can_submit(self, wid: str, urgency: str) -> tuple[bool, str]:
        L = self._limits(wid)
        used = self.used_24h(wid)
        cap = L["total"] if urgency == "incident" else L["routine"]
        if used >= cap:
            return False, f"budget {used}/{cap} used in 24h"
        mod = self.m.now % 1440
        lo, hi = L["hours"]
        earliest = lo if urgency == "incident" else L["earliest"]
        if mod < earliest * 60:
            return False, f"before {earliest}:00"
        # the approver decides at the first working minute after a random latency; a request whose worst-case
        # decision would land after its expiry is a guaranteed "expired", so hold it
        worst = self._next_working(self.m.now + L["latency"][1], lo, hi)
        if worst > self.m.now + L["ttl_h"] * 60:
            return False, "would expire before an approver could decide"
        if urgency != "incident" and mod >= hi * 60:
            return False, "approvers have gone home"
        if urgency == "routine" and mod + L["latency"][1] > hi * 60:
            # Prefer a same-day decision (Compliance: approvers dislike an overnight queue). If this workflow's
            # latency makes that impossible even first thing, submit in the first hour so it is decided early.
            same_day_ever = earliest * 60 + L["latency"][1] <= hi * 60
            if same_day_ever or mod >= earliest * 60 + 60:
                return False, "too late for a same-day decision"
        return True, "ok"

    @staticmethod
    def _next_working(t: int, lo: int, hi: int) -> int:
        day, mod = divmod(t, 1440)
        if mod < lo * 60:
            return day * 1440 + lo * 60
        if mod >= hi * 60:
            return (day + 1) * 1440 + lo * 60
        return t

    def inflight(self, wid: str) -> list:
        return self.s.q("SELECT pid, state, approval_id FROM proposals WHERE wid=? AND route='approval' AND "
                        "state IN ('PREPARED','PENDING','APPROVED')", (wid,))

    # ------------------------------------------------------------------ main entry
    def flush(self, wid: str, intents: list[dict]):
        if not intents:
            return
        m = self.m
        wf = m.wf[wid]
        cur = m.cfg[wid]
        busy = bool(self.inflight(wid))
        auto, appr = [], []
        for it in intents:
            if it.get("auto_ok") and policy.can_auto(wf, it["dims"]):
                if busy and it["urgency"] != "incident":
                    continue   # would make the in-flight approval stale; only incident-level changes overtake
                auto.append(it)
            else:
                appr.append(it)
        if auto:
            self._auto(wid, auto)
            cur = m.cfg[wid]
        if appr:
            urgency = "incident" if any(i["urgency"] == "incident" for i in appr) else \
                "safety" if any(i["urgency"] == "safety" for i in appr) else "routine"
            inflight_kinds = {r[0] for r in self.s.q(
                "SELECT kind FROM proposals WHERE wid=? AND route='approval' AND state IN "
                "('PREPARED','PENDING','APPROVED')", (wid,))}
            if inflight_kinds and not (urgency == "incident" and not any("incident" in k for k in inflight_kinds)):
                return   # one request in flight per workflow; an incident fix may overtake routine work
            ok, why = self.can_submit(wid, urgency)
            if not ok:
                key = f"held:{wid}:{why}:{m.now // 60}"
                if self.s.get("last_hold_note:" + wid) != key:
                    with self.s.tx():
                        self.s.put("last_hold_note:" + wid, key)
                        self.s.decide(m.now, wid, "proposal_held", f"holding {len(appr)} change(s): {why}",
                                      {"kinds": [i["kind"] for i in appr]})
                return
            if urgency == "incident":
                appr = [i for i in appr if i["urgency"] == "incident"] or appr   # incident fixes go alone
            self._approval(wid, appr)

    # ------------------------------------------------------------------ auto-apply path
    def _auto(self, wid: str, intents: list[dict]):
        m = self.m
        wf = m.wf[wid]
        groups = [intents] if len(intents) > 1 else []
        groups += [[i] for i in intents]
        done = set()
        for g in groups:
            if any(id(i) in done for i in g):
                continue
            cur = m.cfg[wid]
            new = compose(cur["config"], g)
            if not policy.changed_dims(cur["config"], new):
                for i in g:
                    done.add(id(i))
                continue
            probs = policy.auto_apply_problems(wf, cur["config"], new) + policy.validate(wf, new)
            if probs:
                continue
            body = {"base_version": cur["version"], "proposed_config": new,
                    "rationale": " | ".join(i["rationale"] for i in g),
                    "evidence": {i["kind"]: i.get("evidence") for i in g},
                    # kept for our audit trail only; not part of the PUT contract
                    "audit": {"change_summary": "; ".join(i["summary"] for i in g),
                              "diff": {i["kind"]: i["diff"] for i in g},
                              "dry_run": {i["kind"]: i["dry_run"] for i in g},
                              "affected_leads": int(max(i.get("affected", 0) for i in g)),
                              "urgency": max((i["urgency"] for i in g), key=["routine", "safety", "incident"].index)}}
            pid = self._prepare(wid, "auto", g, body)
            self._send_auto(pid)
            for i in g:
                done.add(id(i))

    def _send_auto(self, pid: str):
        m = self.m
        row = self.s.q1("SELECT wid, body, intents FROM proposals WHERE pid=?", (pid,))
        wid, body, intents = row[0], json.loads(row[1]), json.loads(row[2])
        try:
            r = self.c.auto_apply(wid, {"proposal_id": pid, **{k: v for k, v in body.items() if k != "audit"}})
        except SimError as e:
            if e.code == "duplicate_proposal_id":
                return self._reconcile_auto(pid)
            state = "STALE" if e.code == "stale_base_version" else "REFUSED"
            with self.s.tx():
                self._set(pid, state, reasons=e.detail)
                self.s.decide(m.now, wid, "auto_apply_failed", f"{pid} {e.code}", {"detail": e.detail}, pid)
                if state == "REFUSED":
                    self.s.alert(m.now, "warn", wid, "auto_apply_refused",
                                 f"auto-apply {pid} refused: {e.detail}",
                                 "policy.py preflight disagrees with the server's governance policy")
                self._hook_failed(wid, intents, state)
            if state == "STALE":
                m.refresh_config(wid)
            return
        self._mark_applied(pid, wid, r["version"], intents, body["proposed_config"], "controller_auto")

    def _reconcile_auto(self, pid: str):
        wid = self.s.q1("SELECT wid FROM proposals WHERE pid=?", (pid,))[0]
        hist = self.c.config_history(wid)
        hit = next((h for h in hist if h.get("proposal_id") == pid), None)
        row = self.s.q1("SELECT body, intents FROM proposals WHERE pid=?", (pid,))
        body, intents = json.loads(row[0]), json.loads(row[1])
        if hit:
            self._mark_applied(pid, wid, hit["version"], intents, body["proposed_config"], "controller_auto")
        else:
            # The id was consumed but no version carries it: the server refused it. Never resend.
            with self.s.tx():
                self._set(pid, "REFUSED", reasons="id consumed without a config version")
                self._hook_failed(wid, intents, "REFUSED")

    # ------------------------------------------------------------------ approval path
    def _approval(self, wid: str, intents: list[dict]):
        m = self.m
        wf = m.wf[wid]
        cur = m.cfg[wid]
        new = compose(cur["config"], intents)
        if not policy.changed_dims(cur["config"], new):
            return
        probs = policy.validate(wf, new)
        if probs:
            with self.s.tx():
                self.s.decide(m.now, wid, "proposal_invalid", "composed config failed preflight", {"problems": probs})
            return
        dims = policy.changed_dims(cur["config"], new)
        impact = 1.0
        for i in intents:
            impact *= 1 - max(0.0, min(1.0, i.get("impact", 0.0)))
        evidence = {i["kind"]: i.get("evidence") for i in intents}
        for i in intents:
            if i.get("validation_run_id"):
                evidence["validation_run_id"] = i["validation_run_id"]
        body = {
            "workflow_id": wid, "base_version": cur["version"], "proposed_config": new,
            "change_summary": "; ".join(i["summary"] for i in intents),
            "diff": {"changed_dims": dims, **{i["kind"]: i["diff"] for i in intents}},
            "affected_leads": int(max(i.get("affected", 0) for i in intents)),
            "dry_run": {i["kind"]: i["dry_run"] for i in intents},
            "impact_score": round(1 - impact, 3),
            "rationale": " | ".join(i["rationale"] for i in intents),
            "evidence": evidence,
        }
        pid = self._prepare(wid, "approval", intents, body)
        self._send_approval(pid)

    def _send_approval(self, pid: str):
        m = self.m
        row = self.s.q1("SELECT wid, body, intents FROM proposals WHERE pid=?", (pid,))
        wid, body, intents = row[0], json.loads(row[1]), json.loads(row[2])
        try:
            r = self.c.submit({"proposal_id": pid, **body})
        except SimError as e:
            if e.code == "duplicate_proposal_id":
                return self._reconcile_approval(pid)
            state = "STALE" if e.code == "stale_base_version" else "INVALID"
            with self.s.tx():
                self._set(pid, state, reasons=e.detail)
                self.s.decide(m.now, wid, "proposal_not_submitted", f"{pid} {e.code}", {"detail": e.detail}, pid)
                if state == "INVALID" and e.code != "no_change":
                    self.s.alert(m.now, "warn", wid, "proposal_invalid", f"{pid}: {e.code} {e.detail}",
                                 "governance.compose / policy.validate")
                self._hook_failed(wid, intents, state)
            if state == "STALE":
                m.refresh_config(wid)
            return
        with self.s.tx():
            self.s.db.execute("UPDATE proposals SET state='PENDING', approval_id=?, submitted_minute=? WHERE pid=?",
                              (r["approval_id"], m.now, pid))
            self.s.decide(m.now, wid, "proposal_submitted",
                          f"{pid} → {r['approval_id']}: {body['change_summary']}",
                          {"impact_score": body["impact_score"], "affected_leads": body["affected_leads"],
                           "expires_at": r.get("expires_at")}, pid)

    def _reconcile_approval(self, pid: str):
        wid = self.s.q1("SELECT wid FROM proposals WHERE pid=?", (pid,))[0]
        hit = next((a for a in self.c.approvals(workflow_id=wid) if a["proposal_id"] == pid), None)
        if hit is None:
            row = self.s.q1("SELECT intents FROM proposals WHERE pid=?", (pid,))
            with self.s.tx():
                self._set(pid, "INVALID", reasons="id consumed but no approval found")
                self._hook_failed(wid, json.loads(row[0]), "INVALID")
            return
        with self.s.tx():
            self.s.db.execute("UPDATE proposals SET state='PENDING', approval_id=?, submitted_minute="
                              "COALESCE(submitted_minute, ?) WHERE pid=?",
                              (hit["approval_id"], self.m.minute_of(hit["created_at"]), pid))
            self.s.decide(self.m.now, wid, "proposal_reconciled", f"{pid} found on server as {hit['approval_id']}",
                          {"status": hit["status"]}, pid)
        self._sync_one(pid, hit)

    # ------------------------------------------------------------------ polling / applying
    def poll(self):
        """Bring every in-flight approval up to date and apply whatever is approved."""
        for pid, state, aid in self.s.q("SELECT pid, state, approval_id FROM proposals WHERE route='approval' AND "
                                        "state IN ('PENDING','APPROVED')"):
            a = self.c.approval(aid)
            self._sync_one(pid, a)

    def _sync_one(self, pid: str, a: dict):
        m = self.m
        row = self.s.q1("SELECT wid, state, intents, body FROM proposals WHERE pid=?", (pid,))
        wid, state, intents, body = row[0], row[1], json.loads(row[2]), json.loads(row[3])
        st = a["status"]
        if st == "pending":
            return
        if st == "applied":
            ver = next((h["version"] for h in self.c.config_history(wid) if h.get("proposal_id") == pid), None)
            if state != "APPLIED":
                self._mark_applied(pid, wid, ver, intents, body["proposed_config"], "controller_approved")
            return
        if st in ("rejected", "expired", "lapsed"):
            with self.s.tx():
                self._set(pid, st.upper(), reasons=a.get("reasons"),
                          decided=m.minute_of(a["decided_at"]) if a.get("decided_at") else m.now)
                self.s.decide(m.now, wid, f"proposal_{st}", f"{pid} {st}: {a.get('reasons')}", a, pid)
                if st == "lapsed":
                    self.s.alert(m.now, "page", wid, "approval_lapsed", f"{pid} was approved but not applied in time",
                                 "loop step size vs apply_window_hours; was the controller down?")
                if st == "rejected" and any("fatigue" in r or "must" in r or "evidence" in r
                                            for r in (a.get("reasons") or [])):
                    self.s.alert(m.now, "warn", wid, "rejected_for_hygiene", f"{pid}: {a.get('reasons')}",
                                 "proposal payload or budget accounting")
                self._hook_failed(wid, intents, st.upper())
            return
        if st == "approved":
            with self.s.tx():
                if state != "APPROVED":
                    self._set(pid, "APPROVED", decided=m.minute_of(a["decided_at"]), reasons=a.get("reasons"))
                    self.s.db.execute("UPDATE proposals SET apply_by_minute=? WHERE pid=?",
                                      (m.minute_of(a["apply_by"]), pid))
                    self.s.decide(m.now, wid, "proposal_approved", f"{pid} approved at {a['decided_at']}", a, pid)
            self._apply(pid, wid, a["approval_id"], intents, body)

    def _apply(self, pid, wid, aid, intents, body):
        m = self.m
        try:
            r = self.c.apply(wid, aid)
        except SimError as e:
            if e.code == "not_approved" and (e.detail or {}).get("status") == "applied":
                return self._sync_one(pid, {"status": "applied"})
            state = {"stale_base_version": "STALE", "approval_lapsed": "LAPSED"}.get(e.code, "INVALID")
            with self.s.tx():
                self._set(pid, state, reasons=e.detail)
                self.s.decide(m.now, wid, "apply_failed", f"{pid} apply {e.code}", {"detail": e.detail}, pid)
                if state == "LAPSED":
                    self.s.alert(m.now, "page", wid, "approval_lapsed", f"{pid} lapsed before apply",
                                 "loop pacing around apply_by")
                self._hook_failed(wid, intents, state)
            m.refresh_config(wid)
            return
        self._mark_applied(pid, wid, r["version"], intents, body["proposed_config"], "controller_approved")

    def _mark_applied(self, pid, wid, version, intents, proposed, actor):
        m = self.m
        with self.s.tx():
            self.s.db.execute("UPDATE proposals SET state='APPLIED', applied_version=?, applied_minute=? WHERE pid=?",
                              (version, m.now, pid))
            self.s.db.execute("INSERT INTO config_versions(wid, version, actor, changed_dims, minute, proposal_id, "
                              "approval_id, config, handled) VALUES(?,?,?,?,?,?,(SELECT approval_id FROM proposals "
                              "WHERE pid=?),?,1) ON CONFLICT(wid, version) DO UPDATE SET config=excluded.config, "
                              "handled=1, proposal_id=excluded.proposal_id",
                              (wid, version, actor, dumps(policy.changed_dims(m.cfg[wid]["config"], proposed)), m.now,
                               pid, pid, dumps(proposed)))
            self.s.decide(m.now, wid, "applied", f"{pid} applied as v{version} ({actor})",
                          {"kinds": [i["kind"] for i in intents]}, pid)
            for i in intents:
                p = self.planners.get(i["kind"])
                if p:
                    p.applied(m, wid, i, version)
        m.refresh_config(wid)

    # ------------------------------------------------------------------ crash recovery
    def reconcile(self):
        """Run at start-up: settle every proposal whose fate we may not have recorded."""
        for pid, route in self.s.q("SELECT pid, route FROM proposals WHERE state='PREPARED'"):
            wid = self.s.q1("SELECT wid FROM proposals WHERE pid=?", (pid,))[0]
            if route == "auto":
                hist = self.c.config_history(wid)
                if any(h.get("proposal_id") == pid for h in hist):
                    self._reconcile_auto(pid)
                else:
                    self._resend_or_drop(pid, wid, self._send_auto)
            else:
                hit = next((a for a in self.c.approvals(workflow_id=wid) if a["proposal_id"] == pid), None)
                if hit:
                    self._reconcile_approval(pid)
                else:
                    self._resend_or_drop(pid, wid, self._send_approval)
        self.poll()

    def _resend_or_drop(self, pid, wid, sender):
        # Never sent (crash between PREPARED and the HTTP call). Resend only if its base is still current.
        row = self.s.q1("SELECT base_version, intents FROM proposals WHERE pid=?", (pid,))
        if row[0] == self.m.cfg[wid]["version"]:
            sender(pid)
        else:
            with self.s.tx():
                self._set(pid, "STALE", reasons="base moved while controller was down")
                self._hook_failed(wid, json.loads(row[1]), "STALE")

    # ------------------------------------------------------------------ helpers
    def _prepare(self, wid: str, route: str, intents: list[dict], body: dict) -> str:
        m = self.m
        with self.s.tx():
            seq = self.s.get("proposal_seq", 0) + 1
            self.s.put("proposal_seq", seq)
            pid = f"{m.s.get('scenario')}:{wid}:{route}:{seq:05d}"
            self.s.db.execute("INSERT INTO proposals(pid, wid, route, kind, state, base_version, body, intents, "
                              "created_minute) VALUES(?,?,?,?,?,?,?,?,?)",
                              (pid, wid, route, "+".join(sorted({i["kind"] for i in intents})), "PREPARED",
                               body["base_version"], dumps(body), dumps(intents), m.now))
        _crash_point("after_prepare")
        return pid

    def _set(self, pid, state, reasons=None, decided=None):
        self.s.db.execute("UPDATE proposals SET state=?, reasons=COALESCE(?, reasons), "
                          "decided_minute=COALESCE(?, decided_minute) WHERE pid=?",
                          (state, dumps(reasons) if reasons is not None else None, decided, pid))

    def _hook_failed(self, wid, intents, state):
        for i in intents:
            p = self.planners.get(i["kind"])
            if p:
                p.failed(self.m, wid, i, state)


def _crash_point(name: str):
    """Fault injection for the crash test: CONTROLLER_CRASH_AT=<name>:<n> kills the process the n-th time."""
    import os
    spec = os.environ.get("CONTROLLER_CRASH_AT")
    if not spec or not spec.startswith(name):
        return
    n = int(spec.split(":")[1]) if ":" in spec else 1
    k = int(os.environ.get("_CRASH_COUNT_" + name, "0")) + 1
    os.environ["_CRASH_COUNT_" + name] = str(k)
    if k >= n:
        log.error("fault injection: crashing at %s", name)
        os._exit(137)
