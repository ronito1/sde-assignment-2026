"""Invariants after a run (used by the crash test). Talks to the simulator API and reads controller.db.

  .venv/bin/python scripts/verify_run.py --url http://127.0.0.1:8700 --db state/x.db
Exit code 0 = every invariant holds.
"""
import argparse
import json
import sqlite3
import sys

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True)
ap.add_argument("--db", required=True)
a = ap.parse_args()
c = httpx.Client(base_url=a.url, timeout=60)
db = sqlite3.connect(a.db)
ok = True


def check(cond, msg):
    global ok
    print(("PASS " if cond else "FAIL ") + msg)
    ok &= bool(cond)


st = c.get("/sim/state").json()
check(st["finished"], f"scenario finished ({st['ts']})")
ours = {pid: (state, aid, ver) for pid, state, aid, ver in
        db.execute("SELECT pid, state, approval_id, applied_version FROM proposals")}
check(not [p for p, v in ours.items() if v[0] == "PREPARED"], "no proposal left in PREPARED (outbox drained)")
for w in c.get("/workflows").json():
    wid = w["workflow_id"]
    hist = c.get(f"/workflows/{wid}/config/history").json()
    ctrl = [h for h in hist if h["actor"].startswith("controller")]
    pids = [h["proposal_id"] for h in ctrl]
    check(len(pids) == len(set(pids)), f"{wid}: no proposal applied twice ({len(pids)} controller versions)")
    unknown = [p for p in pids if p not in ours]
    check(not unknown, f"{wid}: every applied change is in our store")
    wrong = [h for h in ctrl if ours.get(h["proposal_id"], (None, None, None))[2] != h["version"]]
    check(not wrong, f"{wid}: our record of each applied version matches the server")
    server = c.get("/approvals", params={"workflow_id": wid}).json()
    lost = [x["proposal_id"] for x in server if x["proposal_id"] not in ours]
    check(not lost, f"{wid}: no approval lost ({len(server)} on server)")
    mism = [x["approval_id"] for x in server
            if x["status"] == "applied" and ours[x["proposal_id"]][0] != "APPLIED"]
    check(not mism, f"{wid}: every server-applied approval is APPLIED in our store")
restarts = json.loads(db.execute("SELECT v FROM meta WHERE k='restarts'").fetchone()[0])
print(f"controller starts recorded: {restarts}")
sys.exit(0 if ok else 1)
