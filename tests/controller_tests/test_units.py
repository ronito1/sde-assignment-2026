"""Unit tests for the parts where a bug silently corrupts decisions: statistics, governance preflight,
the proposal state machine under crashes/duplicates, and idempotent ingestion.

Run: .venv/bin/python -m pytest -q tests/controller
"""
import copy
import json

import pytest

from controller import policy, stats
from controller.client import SimError
from controller.governance import Governance, compose
from controller.model import Model
from controller.store import Store

WF = {
    "workflow_id": "wf1", "tenant_id": "t1", "dialling_window": [9, 21], "cadence": [2, 2], "mid_flight": "allowed",
    "daily_batch": {"size": 1000, "at_hour": 8}, "objective": {"primary": "engagement_rate",
                                                               "guardrail": {"pickup_rate_max_drop": 0.02}},
    "options": {"welcome_message": ["W1", "W2"], "voice": ["V1", "V2"], "caller_id": ["D1", "D2", "D3"],
                "prompt_version": ["P1", "P3"]},
    "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["caller_id", "schedule"], "max_shift": 0.2},
                   "approver_hours": [10, 19], "approver_latency_minutes": [30, 240], "proposal_ttl_hours": 24,
                   "apply_window_hours": 2, "max_requests_per_24h": 3, "min_validation_pass_rate": 0.97},
}
CFG = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0},
                                                  "caller_id": {"D1": 0.5, "D2": 0.5}}},
       "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


class FakeSim:
    """Just enough of the simulator's HTTP contract to drive governance through crash scenarios."""

    def __init__(self, minute=600):
        self.minute = minute
        self.version = 1
        self.cfg = copy.deepcopy(CFG)
        self.approvals = {}
        self.history = [{"version": 1, "proposal_id": None}]
        self.ids = set()
        self.submits = 0

    def state(self):
        return {"scenario": "fake", "sim_minute": self.minute, "ts": "2027-01-04T10:00:00+05:30",
                "end_minute": 2880, "finished": False, "credits_remaining": 100, "last_event_id": 0}

    def workflows(self):
        return [WF]

    def config(self, wid):
        return {"version": self.version, "config": self.cfg, "actor": "business_default"}

    def config_history(self, wid):
        return self.history

    def chunks(self, kind, after=-1):
        return []

    def submit(self, body):
        if body["proposal_id"] in self.ids:
            raise SimError(409, "duplicate_proposal_id", body["proposal_id"])
        if body["base_version"] != self.version:
            raise SimError(409, "stale_base_version", {})
        self.ids.add(body["proposal_id"])
        self.submits += 1
        aid = f"apr_{len(self.approvals) + 1:05d}"
        self.approvals[aid] = {"approval_id": aid, "proposal_id": body["proposal_id"], "workflow_id": "wf1",
                               "status": "pending", "base_version": body["base_version"], "changed_dims": [],
                               "created_at": "2027-01-04T10:00:00+05:30", "expires_at": "2027-01-05T10:00:00+05:30",
                               "_cfg": body["proposed_config"]}
        return {k: v for k, v in self.approvals[aid].items() if not k.startswith("_")}

    def approvals_list(self, workflow_id=None, status=None):
        return [{k: v for k, v in a.items() if not k.startswith("_")} for a in self.approvals.values()]

    def approval(self, aid):
        return {k: v for k, v in self.approvals[aid].items() if not k.startswith("_")}

    def apply(self, wid, aid):
        a = self.approvals[aid]
        if a["status"] != "approved":
            raise SimError(409, "not_approved", {"status": a["status"]})
        if a["base_version"] != self.version:
            raise SimError(409, "stale_base_version", {})
        self.version += 1
        self.cfg = a["_cfg"]
        a["status"] = "applied"
        self.history.append({"version": self.version, "proposal_id": a["proposal_id"]})
        return {"version": self.version}

    def auto_apply(self, wid, body):
        if body["proposal_id"] in self.ids:
            raise SimError(409, "duplicate_proposal_id", body["proposal_id"])
        self.ids.add(body["proposal_id"])
        self.version += 1
        self.cfg = body["proposed_config"]
        self.history.append({"version": self.version, "proposal_id": body["proposal_id"]})
        return {"version": self.version}

    def decide(self, aid, status):
        self.approvals[aid]["status"] = status
        self.approvals[aid]["decided_at"] = "2027-01-04T11:00:00+05:30"
        self.approvals[aid]["apply_by"] = "2027-01-04T13:00:00+05:30"
        self.approvals[aid]["reasons"] = []


FakeSim.approvals_attr = None


def _client(sim):
    class C:
        pass
    c = C()
    for name in ("state", "workflows", "config", "config_history", "chunks", "submit", "approval", "apply",
                 "auto_apply"):
        setattr(c, name, getattr(sim, name))
    c.approvals = sim.approvals_list
    return c


class Recorder:
    def __init__(self):
        self.applied, self.failed = [], []

    def applied_hook(self, *a):
        self.applied.append(a)


def _setup(tmp_path, sim):
    store = Store(tmp_path / "c.db")
    m = Model(store, _client(sim))
    m.boot()
    rec = {"applied": [], "failed": []}

    class P:
        def applied(self, m, wid, it, v):
            rec["applied"].append(v)

        def failed(self, m, wid, it, state):
            rec["failed"].append(state)
    gov = Governance(m, {"test": P()})
    return store, m, gov, rec


def _intent(dims=("welcome_message",), urgency="routine", auto_ok=False, d=None):
    return {"key": "k", "kind": "test", "ops": [("alloc", "default", dims[0], d or {"W1": 0.7, "W2": 0.3})],
            "dims": list(dims), "urgency": urgency, "auto_ok": auto_ok, "summary": "s", "diff": {"x": 1},
            "rationale": "r", "affected": 10, "dry_run": {"ok": 1}, "impact": 0.3, "evidence": {}, "meta": {}}


# ------------------------------------------------------------------------------------------- statistics
def test_two_prop_symmetry_and_direction():
    t = stats.two_prop(60, 100, 50, 100)
    assert t["diff"] == pytest.approx(0.10)
    assert t["p_better"] < 0.1 < t["p_worse"]
    assert stats.two_prop(0, 0, 5, 10)["p_better"] == 1.0


def test_holm_step_down():
    assert stats.holm({"a": 0.001, "b": 0.03, "c": 0.04}, 0.05) == {"a"}
    assert stats.holm({"a": 0.001, "b": 0.02, "c": 0.04}, 0.05) == {"a", "b", "c"}
    assert stats.holm({"a": 0.001, "b": 0.02}, 0.05) == {"a", "b"}


def test_binom_sf_matches_known_values():
    assert stats.binom_sf(0, 10, 0.5) == 1.0
    assert stats.binom_sf(10, 10, 0.5) == pytest.approx(0.5 ** 10)
    assert stats.binom_sf(14, 322, 0.0153) < 1e-3   # the public_02 bad-prompt detection


def test_sample_size_for_five_points():
    n = stats.n_per_arm(0.55, 0.60)
    assert 1450 < n < 1600


# ------------------------------------------------------------------------------------------- preflight
def test_changed_dims_and_shift_match_contract():
    new = copy.deepcopy(CFG)
    new["allocations"]["default"]["caller_id"] = {"D1": 0.4, "D2": 0.4, "D3": 0.2}
    assert policy.changed_dims(CFG, new) == ["caller_id"]
    assert policy.max_shift(CFG, new) == pytest.approx(0.2)
    assert policy.auto_apply_problems(WF, CFG, new) == []
    new["allocations"]["default"]["caller_id"] = {"D3": 1.0}
    assert "shift too large" in policy.auto_apply_problems(WF, CFG, new)


def test_step_towards_respects_limit():
    step = policy.step_towards({"D1": 1.0}, {"D2": 1.0}, 0.2)
    assert stats.tv({"D1": 1.0}, step) <= 0.2 + 1e-9


def test_validate_catches_missing_hours_and_unknown_cohorts():
    bad = copy.deepcopy(CFG)
    bad["schedule"]["hour_weights"] = {"young": {"8": 1}}
    probs = policy.validate(WF, bad)
    assert any("undefined cohort" in p for p in probs) and any("outside window" in p for p in probs)


def test_compose_puts_cohorts_before_references():
    cfg = compose(CFG, [{"ops": [("hour_weights", "old", {"10": 2.0}), ("cohorts", [{"id": "old",
                                                                                    "where": {"age_band": ["60+"]}}])]}])
    assert cfg["cohorts"][0]["id"] == "old" and policy.validate(WF, cfg) == []


# ------------------------------------------------------------------------------------------- governance
def test_submit_poll_apply_happy_path(tmp_path):
    sim = FakeSim()
    store, m, gov, rec = _setup(tmp_path, sim)
    gov.flush("wf1", [_intent()])
    assert sim.submits == 1
    aid = next(iter(sim.approvals))
    sim.decide(aid, "approved")
    gov.poll()
    assert rec["applied"] == [2]
    assert store.q1("SELECT state FROM proposals")[0] == "APPLIED"


def test_crash_after_prepare_before_send_is_resent_once(tmp_path):
    sim = FakeSim()
    store, m, gov, rec = _setup(tmp_path, sim)
    body = {"workflow_id": "wf1", "base_version": 1, "proposed_config": compose(CFG, [_intent()]),
            "change_summary": "s", "diff": {"x": 1}, "affected_leads": 1, "dry_run": {"a": 1},
            "impact_score": 0.1, "rationale": "r", "evidence": {}}
    pid = gov._prepare("wf1", "approval", [_intent()], body)   # crash right here: nothing sent
    gov2 = Governance(m, gov.planners)
    gov2.reconcile()
    assert sim.submits == 1
    assert store.q1("SELECT state FROM proposals WHERE pid=?", (pid,))[0] == "PENDING"
    gov2.reconcile()                                            # a second restart changes nothing
    assert sim.submits == 1


def test_crash_after_send_before_record_is_adopted_not_duplicated(tmp_path):
    sim = FakeSim()
    store, m, gov, rec = _setup(tmp_path, sim)
    body = {"workflow_id": "wf1", "base_version": 1, "proposed_config": compose(CFG, [_intent()]),
            "change_summary": "s", "diff": {"x": 1}, "affected_leads": 1, "dry_run": {"a": 1},
            "impact_score": 0.1, "rationale": "r", "evidence": {}}
    pid = gov._prepare("wf1", "approval", [_intent()], body)
    sim.submit({"proposal_id": pid, **body})                   # sent, then the process died
    Governance(m, gov.planners).reconcile()
    assert sim.submits == 1
    row = store.q1("SELECT state, approval_id FROM proposals WHERE pid=?", (pid,))
    assert row == ("PENDING", "apr_00001")


def test_crash_after_apply_before_record(tmp_path):
    sim = FakeSim()
    store, m, gov, rec = _setup(tmp_path, sim)
    gov.flush("wf1", [_intent()])
    aid = next(iter(sim.approvals))
    sim.decide(aid, "approved")
    sim.apply("wf1", aid)                                       # applied on the server, crash before recording
    Governance(m, gov.planners).reconcile()
    assert store.q1("SELECT state, applied_version FROM proposals")[0:2] == ("APPLIED", 2)
    assert sim.version == 2                                     # never applied twice


def test_supervisor_edit_makes_apply_stale_and_hooks_failure(tmp_path):
    sim = FakeSim()
    store, m, gov, rec = _setup(tmp_path, sim)
    gov.flush("wf1", [_intent()])
    aid = next(iter(sim.approvals))
    sim.decide(aid, "approved")
    sim.version += 1                                            # supervisor edit
    gov.poll()
    assert store.q1("SELECT state FROM proposals")[0] == "STALE"
    assert rec["failed"] == ["STALE"]


def test_one_request_in_flight_and_timing(tmp_path):
    sim = FakeSim(minute=600)
    store, m, gov, rec = _setup(tmp_path, sim)
    gov.flush("wf1", [_intent()])
    gov.flush("wf1", [_intent(("voice",), d={"V1": 0.7, "V2": 0.3})])
    assert sim.submits == 1
    m.now = 1440 + 9 * 60                                       # before approver hours
    assert gov.can_submit("wf1", "routine")[0] is False
    m.now = 1440 + 17 * 60                                      # 17:00 + 240 min latency → next-day decision
    assert gov.can_submit("wf1", "routine")[0] is False


def test_auto_apply_used_only_when_policy_allows(tmp_path):
    sim = FakeSim()
    store, m, gov, rec = _setup(tmp_path, sim)
    gov.flush("wf1", [_intent(("caller_id",), urgency="safety", auto_ok=True, d={"D1": 0.4, "D2": 0.4, "D3": 0.2})])
    assert sim.version == 2 and sim.submits == 0
    assert store.q1("SELECT route, state FROM proposals")[0:2] == ("auto", "APPLIED")


# ------------------------------------------------------------------------------------------- ingestion
def test_ingest_is_idempotent(tmp_path):
    sim = FakeSim()
    lines = [
        {"type": "lead", "ts": "2027-01-04T08:00:00+05:30", "workflow_id": "wf1", "campaign_id": "wf1:c1",
         "lead_id": "wf1:l1", "person_id": "wf1:p1",
         "features": {"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"}, "event_id": 1},
        {"type": "call", "ts": "2027-01-04T10:07:00+05:30", "workflow_id": "wf1", "campaign_id": "wf1:c1",
         "lead_id": "wf1:l1", "person_id": "wf1:p1", "attempt_no": 1, "campaign_day_index": 0, "config_version": 1,
         "cohort_id": "default", "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1",
                                             "prompt_version": "P1"}, "connected": True, "engaged": True,
         "qa": {"hallucination_flag": False}, "event_id": 2},
    ]
    text = "\n".join(json.dumps(x) for x in lines) + "\n"
    meta = [{"chunk": 0, "kind": "events"}]
    sim.chunks = lambda kind, after=-1: [c for c in meta if c["kind"] == kind and c["chunk"] > after]
    c = _client(sim)
    c.chunk_text = lambda n: text
    store = Store(tmp_path / "i.db")
    m = Model(store, c)
    m.boot()
    assert m.ingest() == 2
    assert m.ingest() == 0                                      # cursor moved: nothing re-read
    store.put("last_chunk", -1)                                 # simulate re-delivery of the same chunk
    assert m.ingest() == 0                                      # event ids at or below the cursor are skipped
    assert m.calls.n == 1 and m.calls["minute"][0] == 607 and m.calls["fc"][0] > 0
