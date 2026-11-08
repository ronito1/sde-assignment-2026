"""Durable state: one SQLite file (WAL). Everything the controller knows or decided lives here.

Crash model: the process can die at any instruction. Every unit of work that must be atomic (ingesting a
chunk together with the cursor, moving a proposal to a new state together with its HTTP result) happens in
one transaction. On restart, in-memory structures are rebuilt from these tables.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS strs(i INTEGER PRIMARY KEY, s TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS workflows(wid TEXT PRIMARY KEY, spec TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fcombos(fc INTEGER PRIMARY KEY, lang TEXT, city_tier TEXT, age_band TEXT,
    value_band TEXT, UNIQUE(lang, city_tier, age_band, value_band));
CREATE TABLE IF NOT EXISTS campaigns(campaign_id TEXT PRIMARY KEY, wid TEXT, minute INT, ver INT, leads INT);
CREATE TABLE IF NOT EXISTS leads(lead_id TEXT PRIMARY KEY, wid TEXT, campaign_id TEXT, person_id TEXT,
    fc INT, minute INT);
-- one row per dial: the evidence base for learning, and the answer to "what did this customer hear"
CREATE TABLE IF NOT EXISTS calls(event_id INTEGER PRIMARY KEY, w INT, minute INT, fc INT, att INT, ver INT,
    camp INT, wm INT, vo INT, ci INT, pv INT, conn INT, eng INT, hf INT, hist INT, lead_id TEXT,
    person_id TEXT);
CREATE TABLE IF NOT EXISTS config_versions(wid TEXT, version INT, actor TEXT, changed_dims TEXT,
    minute INT, proposal_id TEXT, approval_id TEXT, config TEXT, handled INT DEFAULT 0,
    PRIMARY KEY(wid, version));
CREATE TABLE IF NOT EXISTS cohorts(wid TEXT, cid TEXT, ord INT, where_json TEXT, owner TEXT, basis TEXT,
    created_minute INT, PRIMARY KEY(wid, cid));
CREATE TABLE IF NOT EXISTS proposals(pid TEXT PRIMARY KEY, wid TEXT, route TEXT, kind TEXT, state TEXT,
    base_version INT, body TEXT, intents TEXT, approval_id TEXT, created_minute INT, submitted_minute INT,
    decided_minute INT, apply_by_minute INT, applied_version INT, applied_minute INT, reasons TEXT,
    note TEXT);
CREATE TABLE IF NOT EXISTS experiments(eid TEXT PRIMARY KEY, wid TEXT, dim TEXT, cohort TEXT, state TEXT,
    control TEXT, arms TEXT, start_minute INT, champion TEXT, promoted_minute INT, ended_minute INT,
    stats TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS incidents(iid TEXT PRIMARY KEY, wid TEXT, kind TEXT, state TEXT, opened_minute INT,
    since_minute INT, suspect TEXT, evidence TEXT, fix TEXT, closed_minute INT, note TEXT);
CREATE TABLE IF NOT EXISTS credits(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INT, wid TEXT, kind TEXT,
    cost REAL, ref TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INT, wid TEXT, kind TEXT,
    summary TEXT, data TEXT, pid TEXT);
CREATE INDEX IF NOT EXISTS decisions_wid ON decisions(wid, minute);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INT, severity TEXT, wid TEXT,
    code TEXT, message TEXT, look_at TEXT);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INT, wall REAL,
    events INT, ingest_s REAL, decide_s REAL, advance_s REAL, rss_mb REAL, credits REAL, lag_events INT);
"""

CALL_COLS = ("w", "minute", "fc", "att", "ver", "camp", "wm", "vo", "ci", "pv", "conn", "eng", "hf", "hist")
_DT = {"w": "i2", "minute": "i4", "fc": "i2", "att": "i1", "ver": "i2", "camp": "i4", "wm": "i2", "vo": "i2",
       "ci": "i2", "pv": "i2", "conn": "?", "eng": "?", "hf": "?", "hist": "?"}


class CallLog:
    """Column store of every dial, in memory, for fast numpy roll-ups. Rebuilt from `calls` on restart."""

    def __init__(self):
        self.n = 0
        self._cap = 1 << 16
        self.c = {k: np.zeros(self._cap, dtype=d) for k, d in _DT.items()}

    def extend(self, rows: list[tuple]):
        k = len(rows)
        if not k:
            return
        while self.n + k > self._cap:
            self._cap *= 2
            for name, arr in self.c.items():
                new = np.zeros(self._cap, dtype=arr.dtype)
                new[: self.n] = arr[: self.n]
                self.c[name] = new
        cols = list(zip(*rows))
        for name, col in zip(CALL_COLS, cols):
            self.c[name][self.n: self.n + k] = col
        self.n += k

    def __getitem__(self, name) -> np.ndarray:
        return self.c[name][: self.n]


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # WAL+NORMAL is durable against process crashes
        self.db.executescript(SCHEMA)
        self._depth = 0
        self._strs: dict[str, int] = {s: i for i, s in self.db.execute("SELECT i, s FROM strs")}
        self._strs_rev: dict[int, str] = {i: s for s, i in self._strs.items()}

    # ------------------------------------------------------------ transactions
    @contextmanager
    def tx(self):
        if self._depth:
            self._depth += 1
            try:
                yield self.db
            finally:
                self._depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.db
        except BaseException:
            self._depth = 0
            self.db.execute("ROLLBACK")
            raise
        self._depth = 0
        self.db.execute("COMMIT")

    def q(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        return self.db.execute(sql, args).fetchone()

    # ------------------------------------------------------------ meta
    def get(self, k: str, default=None):
        r = self.q1("SELECT v FROM meta WHERE k=?", (k,))
        return default if r is None else json.loads(r[0])

    def put(self, k: str, v):
        self.db.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v)))

    # ------------------------------------------------------------ string interning (variant values, ids)
    def intern(self, s: str) -> int:
        i = self._strs.get(s)
        if i is None:
            cur = self.db.execute("INSERT INTO strs(s) VALUES(?)", (s,))
            i = cur.lastrowid
            self._strs[s] = i
            self._strs_rev[i] = s
        return i

    def code(self, s: str) -> int:
        """Code of an already-seen string, or -1."""
        return self._strs.get(s, -1)

    def name(self, i: int) -> str:
        return self._strs_rev.get(int(i), "?")

    # ------------------------------------------------------------ audit
    def decide(self, minute: int, wid: str | None, kind: str, summary: str, data=None, pid: str | None = None):
        self.db.execute("INSERT INTO decisions(minute, wid, kind, summary, data, pid) VALUES(?,?,?,?,?,?)",
                        (minute, wid, kind, summary, json.dumps(data, default=_jd) if data is not None else None,
                         pid))

    def alert(self, minute: int, severity: str, wid: str | None, code: str, message: str, look_at: str):
        self.db.execute("INSERT INTO alerts(minute, severity, wid, code, message, look_at) VALUES(?,?,?,?,?,?)",
                        (minute, severity, wid, code, message, look_at))

    def wipe(self):
        tables = [r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table' "
                                       "AND name NOT LIKE 'sqlite_%'")]
        with self.tx():
            for t in tables:
                self.db.execute(f"DELETE FROM {t}")
        self._strs.clear()
        self._strs_rev.clear()

    def load_calls(self) -> CallLog:
        log = CallLog()
        cur = self.db.execute(f"SELECT {', '.join(CALL_COLS)} FROM calls ORDER BY event_id")
        while True:
            rows = cur.fetchmany(200_000)
            if not rows:
                break
            log.extend(rows)
        return log


def _jd(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


def dumps(o) -> str:
    return json.dumps(o, default=_jd, sort_keys=True)
