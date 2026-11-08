"""What the controller knows about the world, rebuilt from the store and the API on every start.

Ingestion (C1) lives here too: each chunk is parsed and committed in ONE transaction together with the
cursor, so a crash at any point either keeps the whole chunk or none of it. Events with an event_id at or
below the cursor are skipped, which makes re-delivery harmless.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

import numpy as np

from .client import SimClient
from .store import CALL_COLS, Store, dumps

log = logging.getLogger("controller.model")
FEATURES = ("lang", "city_tier", "age_band", "value_band")
MIN_PER_DAY = 1440


class Model:
    def __init__(self, store: Store, client: SimClient):
        self.s, self.c = store, client
        self.wf: dict[str, dict] = {}          # wid -> public workflow spec
        self.cfg: dict[str, dict] = {}         # wid -> {"version", "config", "actor"}
        self.fcombo: dict[tuple, int] = {}
        self.fc_feat: list[dict] = [{}]        # fc 0 = unknown lead
        self.lead_fc: dict[str, int] = {}
        self.calls = None
        self.base: dt.datetime | None = None
        self.now = 0
        self._ts_cache: dict[str, int] = {}
        self.ingest_stats = {"events": 0, "seconds": 0.0}

    # ------------------------------------------------------------------ bootstrap
    def boot(self):
        st = self.c.state()
        scenario = self.s.get("scenario")
        stored_last = self.s.get("last_event_id", 0)
        stored_world = self.s.get("world_fingerprint")
        wfs = self.c.workflows()
        fp = {"scenario": st["scenario"], "end_minute": st["end_minute"],
              "workflows": sorted(w["workflow_id"] for w in wfs)}
        if scenario is not None and (stored_world != fp or st["last_event_id"] < stored_last):
            log.warning("simulator world changed (%s -> %s); starting from an empty store", stored_world, fp)
            self.s.wipe()
        with self.s.tx():
            self.s.put("scenario", st["scenario"])
            self.s.put("world_fingerprint", fp)
            for w in wfs:
                self.s.db.execute("INSERT INTO workflows(wid, spec) VALUES(?, ?) ON CONFLICT(wid) DO UPDATE "
                                  "SET spec=excluded.spec", (w["workflow_id"], dumps(w)))
                self.s.intern(w["workflow_id"])
        self.wf = {w["workflow_id"]: w for w in wfs}
        ts = dt.datetime.fromisoformat(st["ts"])
        self.base = ts - dt.timedelta(minutes=st["sim_minute"])
        self.now = st["sim_minute"]
        self.end = st["end_minute"]
        for fc, *vals in self.s.q("SELECT fc, lang, city_tier, age_band, value_band FROM fcombos ORDER BY fc"):
            self.fcombo[tuple(vals)] = fc
            while len(self.fc_feat) <= fc:
                self.fc_feat.append({})
            self.fc_feat[fc] = dict(zip(FEATURES, vals))
        t0 = time.time()
        self.calls = self.s.load_calls()
        self._reload_leads()
        log.info("restored %d calls, %d live leads in %.1fs", self.calls.n, len(self.lead_fc), time.time() - t0)
        for wid in self.wf:
            self.refresh_config(wid)

    def _reload_leads(self):
        horizon = self.now - 6 * MIN_PER_DAY
        self.lead_fc = dict(self.s.q("SELECT lead_id, fc FROM leads WHERE minute >= ?", (horizon,)))

    def refresh_config(self, wid: str):
        cur = self.c.config(wid)
        self.cfg[wid] = {"version": cur["version"], "config": cur["config"], "actor": cur["actor"]}
        with self.s.tx():
            row = self.s.q1("SELECT config FROM config_versions WHERE wid=? AND version=?", (wid, cur["version"]))
            if row is None:
                # versions we never saw an event for (e.g. the business default) are recorded as handled
                self.s.db.execute("INSERT INTO config_versions(wid, version, actor, changed_dims, minute, "
                                  "proposal_id, approval_id, config, handled) VALUES(?,?,?,?,?,?,?,?,1)",
                                  (wid, cur["version"], cur["actor"], "[]", self.now, None, None,
                                   dumps(cur["config"])))
            elif row[0] is None:
                self.s.db.execute("UPDATE config_versions SET config=? WHERE wid=? AND version=?",
                                  (dumps(cur["config"]), wid, cur["version"]))
        return self.cfg[wid]

    # ------------------------------------------------------------------ helpers
    def minute_of(self, ts: str) -> int:
        m = self._ts_cache.get(ts)
        if m is None:
            m = int((dt.datetime.fromisoformat(ts) - self.base).total_seconds() // 60)
            if len(self._ts_cache) > 50_000:
                self._ts_cache.clear()
            self._ts_cache[ts] = m
        return m

    def fmt(self, minute: int) -> str:
        d, m = divmod(int(minute), MIN_PER_DAY)
        return f"D{d} {m // 60:02d}:{m % 60:02d}"

    def tenant(self, wid: str) -> str:
        return self.wf[wid]["tenant_id"]

    def wcode(self, wid: str) -> int:
        return self.s.code(wid)

    def _fc(self, feats: dict) -> int:
        key = tuple(str(feats.get(f)) for f in FEATURES)
        fc = self.fcombo.get(key)
        if fc is None:
            fc = len(self.fc_feat)
            self.s.db.execute("INSERT INTO fcombos(fc, lang, city_tier, age_band, value_band) VALUES(?,?,?,?,?)",
                              (fc, *key))
            self.fcombo[key] = fc
            self.fc_feat.append(dict(zip(FEATURES, key)))
        return fc

    def fc_matrix(self) -> dict[str, np.ndarray]:
        """feature -> array over fc of the value (object), for vectorised cohort masks."""
        return {f: np.array([d.get(f) for d in self.fc_feat], dtype=object) for f in FEATURES}

    # ------------------------------------------------------------------ ingestion (C1)
    def ingest(self) -> int:
        """Fetch and commit every chunk after the cursor. Returns the number of new events."""
        last_chunk = self.s.get("last_chunk", -1)
        metas = self.c.chunks("history", last_chunk) + self.c.chunks("events", last_chunk)
        metas.sort(key=lambda m: m["chunk"])
        total = 0
        for meta in metas:
            total += self._ingest_chunk(meta)
        return total

    def _ingest_chunk(self, meta: dict) -> int:
        t0 = time.time()
        text = self.c.chunk_text(meta["chunk"])
        hist = 1 if meta["kind"] == "history" else 0
        last_eid = self.s.get("last_event_id", 0)
        call_rows, lead_rows, camp_rows, cfg_rows, decided = [], [], [], [], []
        n = 0
        s = self.s
        with s.tx():
            for line in text.splitlines():
                if not line:
                    continue
                ev = json.loads(line)
                eid = ev["event_id"]
                if eid <= last_eid:
                    continue
                n += 1
                t = ev["type"]
                if t == "call":
                    v = ev["variant"]
                    lead = ev["lead_id"]
                    fc = self.lead_fc.get(lead)
                    if fc is None:
                        r = s.q1("SELECT fc FROM leads WHERE lead_id=?", (lead,))
                        fc = r[0] if r else 0
                    qa = ev.get("qa") or {}
                    call_rows.append((eid, s.intern(ev["workflow_id"]), self.minute_of(ev["ts"]), fc,
                                      min(int(ev["attempt_no"]), 127), int(ev["config_version"]),
                                      s.intern(ev["campaign_id"]), s.intern(v["welcome_message"]),
                                      s.intern(v["voice"]), s.intern(v["caller_id"]), s.intern(v["prompt_version"]),
                                      bool(ev["connected"]), bool(ev["engaged"]),
                                      bool(qa.get("hallucination_flag")), bool(hist), lead, ev["person_id"]))
                elif t == "lead":
                    fc = self._fc(ev["features"])
                    self.lead_fc[ev["lead_id"]] = fc
                    lead_rows.append((ev["lead_id"], ev["workflow_id"], ev["campaign_id"], ev["person_id"], fc,
                                      self.minute_of(ev["ts"])))
                elif t == "campaign_created":
                    camp_rows.append((ev["campaign_id"], ev["workflow_id"], self.minute_of(ev["ts"]),
                                      ev["config_version_at_creation"], ev["leads"]))
                elif t == "config_changed":
                    cfg_rows.append((ev["workflow_id"], ev["version"], ev["actor"], json.dumps(ev["changed_dims"]),
                                     self.minute_of(ev["ts"]), ev.get("proposal_id"), ev.get("approval_id")))
                elif t == "approval_decided":
                    decided.append(ev)
                last_eid = eid
            if camp_rows:
                s.db.executemany("INSERT OR IGNORE INTO campaigns VALUES(?,?,?,?,?)", camp_rows)
            if lead_rows:
                s.db.executemany("INSERT OR IGNORE INTO leads VALUES(?,?,?,?,?,?)", lead_rows)
            if call_rows:
                s.db.executemany(f"INSERT OR IGNORE INTO calls(event_id, {', '.join(CALL_COLS)}, lead_id, person_id)"
                                 f" VALUES({','.join('?' * (len(CALL_COLS) + 3))})", call_rows)
            for r in cfg_rows:
                # history-era versions are facts but need no reaction
                s.db.execute("INSERT INTO config_versions(wid, version, actor, changed_dims, minute, proposal_id, "
                             "approval_id, handled) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(wid, version) DO NOTHING",
                             (*r, hist))
            for ev in decided:
                m = self.minute_of(ev["ts"])
                s.db.execute("UPDATE proposals SET state=?, decided_minute=?, reasons=? WHERE approval_id=? "
                             "AND state='PENDING'", (ev["status"].upper(), m, json.dumps(ev.get("reasons") or []),
                                                     ev["approval_id"]))
            s.put("last_event_id", last_eid)
            s.put("last_chunk", meta["chunk"])
        self.calls.extend([r[1:15] for r in call_rows])
        self.ingest_stats["events"] += n
        self.ingest_stats["seconds"] += time.time() - t0
        log.debug("chunk %d: %d events in %.2fs", meta["chunk"], n, time.time() - t0)
        if meta["kind"] == "events" and lead_rows:
            # evict leads of campaigns that can no longer be dialled
            if len(self.lead_fc) > 400_000:
                self._reload_leads()
        return n
