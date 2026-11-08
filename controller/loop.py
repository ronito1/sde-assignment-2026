"""The orchestrator. One iteration:

    ingest new chunks → react to config changes (supervisor edits) → poll / apply approvals →
    detectors and planners → governance flush → metrics → advance the clock

Restart behaviour: everything is rebuilt from the store, in-flight proposals are reconciled against the
server by proposal_id, and the loop continues from whatever minute the simulator is at.
"""
from __future__ import annotations

import json
import logging
import resource
import sys
import time

from . import cohorts as cohorts_mod
from . import settings
from .client import SimClient, SimError, SimUnavailable
from .governance import Governance, _crash_point
from .model import Model
from .planners.caller_id import CallerIdPlanner
from .planners.experiments import ExperimentPlanner
from .planners.incidents import IncidentPlanner
from .planners.schedule import SchedulePlanner
from .store import Store

log = logging.getLogger("controller.loop")


class Controller:
    def __init__(self, url: str, db: str):
        self.client = SimClient(url)
        self.store = Store(db)
        self.m = Model(self.store, self.client)
        self.incidents = IncidentPlanner()
        self.dids = CallerIdPlanner()
        self.experiments = ExperimentPlanner()
        self.schedule = SchedulePlanner()
        self.planners = {"incident": self.incidents, "caller_id": self.dids, "experiment": self.experiments,
                         "schedule": self.schedule}
        self.gov = Governance(self.m, self.planners)

    # ------------------------------------------------------------------ lifecycle
    def boot(self):
        self._wait_for_sim()
        self.m.boot()
        n = self.m.ingest()
        self.m.now = self.client.state()["sim_minute"]
        log.info("booted at %s; ingested %d new events; %d calls known", self.m.fmt(self.m.now), n, self.m.calls.n)
        for wid in self.m.wf:
            cohorts_mod.choose(self.m, wid)
            self.incidents.baseline(self.m, wid)
        with self.store.tx():
            self.store.decide(self.m.now, None, "controller_started",
                              f"controller (re)started at {self.m.fmt(self.m.now)}",
                              {"calls": self.m.calls.n, "restarts": self.store.get("restarts", 0)})
            self.store.put("restarts", self.store.get("restarts", 0) + 1)
        self.gov.reconcile()
        self._handle_config_events()

    def _wait_for_sim(self):
        for i in range(120):
            try:
                self.client.health()
                return
            except (SimUnavailable, SimError):
                time.sleep(1)
        raise SystemExit("simulator not reachable")

    def run(self):
        self.boot()
        last_plan = self.store.get("last_plan_minute", -10 ** 9)
        while True:
            t0 = time.time()
            st = self.client.state()
            self.m.now = st["sim_minute"]
            n = self.m.ingest()
            t_ing = time.time() - t0
            self._handle_config_events()
            self.gov.poll()
            hourly = self.m.now - last_plan >= settings.PLAN_EVERY_MIN
            for wid in self.m.wf:
                intents = []
                intents += self.incidents.plan(self.m, wid, self.gov)
                intents += self.dids.plan(self.m, wid, self.gov)
                if hourly:
                    intents += self.schedule.plan(self.m, wid, self.gov)
                    intents += self.experiments.plan(self.m, wid, self.gov)
                self.gov.flush(wid, intents)
            if hourly:
                last_plan = self.m.now
                with self.store.tx():
                    self.store.put("last_plan_minute", last_plan)
            t_dec = time.time() - t0 - t_ing
            if st["finished"]:
                self._metrics(n, t_ing, t_dec, 0.0, st)
                break
            step = self._step()
            ta = time.time()
            self._advance(step)
            self._metrics(n, t_ing, t_dec, time.time() - ta, st)
        with self.store.tx():
            self.store.decide(self.m.now, None, "controller_finished", "scenario finished", {})
        log.info("scenario finished at %s", self.m.fmt(self.m.now))

    # ------------------------------------------------------------------ reactions
    def _handle_config_events(self):
        rows = self.store.q("SELECT wid, version, actor, changed_dims FROM config_versions WHERE handled=0 "
                            "ORDER BY minute, version")
        for wid, version, actor, dims in rows:
            dims = json.loads(dims or "[]")
            cur = self.m.refresh_config(wid)
            with self.store.tx():
                if actor == "supervisor":
                    self.store.decide(self.m.now, wid, "supervisor_edit",
                                      f"supervisor changed {dims} (v{version}); not reverted", {"version": version})
                    cohorts_mod.adopt_supervisor_cohorts(self.m, wid, cur["config"])
                    for p in (self.experiments, self.incidents):
                        p.supervisor_edit(self.m, wid, dims, version)
                self.store.db.execute("UPDATE config_versions SET handled=1 WHERE wid=? AND version=?", (wid, version))

    # ------------------------------------------------------------------ pacing
    def _step(self) -> int:
        m = self.m
        busy = self.store.q1("SELECT COUNT(*) FROM proposals WHERE state IN ('PENDING','APPROVED','PREPARED')")[0]
        open_inc = self.store.q1("SELECT COUNT(*) FROM incidents WHERE state != 'RESOLVED'")[0]
        mod = m.now % 1440
        hour = mod // 60
        active = any(w["dialling_window"][0] <= hour < w["dialling_window"][1] or
                     w["governance"]["approver_hours"][0] <= hour < w["governance"]["approver_hours"][1]
                     for w in m.wf.values())
        if busy or open_inc:
            step = settings.STEP_BUSY if active else self._until_next_active()
        elif active:
            step = settings.STEP_IDLE - (mod % settings.STEP_IDLE) or settings.STEP_IDLE
        else:
            step = self._until_next_active()
        return max(settings.TICK, min(step, m.end - m.now))

    def _until_next_active(self) -> int:
        m = self.m
        starts = set()
        for w in m.wf.values():
            starts |= {w["dialling_window"][0], w["governance"]["approver_hours"][0], w["daily_batch"]["at_hour"]}
        mod = m.now % 1440
        best = min(((h * 60 - mod) % 1440) or 1440 for h in starts)
        return max(settings.TICK, best - best % settings.TICK)

    def _advance(self, minutes: int):
        with self.store.tx():
            self.store.put("advance_intent", {"from": self.m.now, "minutes": minutes})
        _crash_point("before_advance")
        try:
            r = self.client.advance(minutes)
            self.m.now = r["sim_minute"]
        except SimError as e:
            if e.code != "finished":
                raise
        except SimUnavailable:
            # The request may or may not have moved time. Never re-send blindly: the next iteration reads
            # /sim/state and carries on from wherever the world actually is.
            log.warning("advance failed in transit; re-reading state")
            self._wait_for_sim()

    def _metrics(self, n, t_ing, t_dec, t_adv, st):
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
        with self.store.tx():
            self.store.db.execute("INSERT INTO metrics(minute, wall, events, ingest_s, decide_s, advance_s, rss_mb, "
                                  "credits, lag_events) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (self.m.now, time.time(), n, t_ing, t_dec, t_adv, rss_mb, st["credits_remaining"],
                                   st["last_event_id"] - self.store.get("last_event_id", 0)))
        if n and self.m.now % 1440 == 0:
            log.info("%s  events=%d ingest=%.1fs decide=%.1fs rss=%.0fMB credits=%.0f", self.m.fmt(self.m.now), n,
                     t_ing, t_dec, rss_mb, st["credits_remaining"])
