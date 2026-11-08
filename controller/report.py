"""Read-only views over the store: the audit answer (C12), the supervisor screen (C11) and loop health (C14).

None of this talks to the simulator; it only reads controller.db, so it works after the run, or while the
controller is running (SQLite WAL allows concurrent readers).
"""
from __future__ import annotations

import json
import sqlite3

MIN_PER_DAY = 1440


def fmt(minute) -> str:
    if minute is None:
        return "—"
    d, m = divmod(int(minute), MIN_PER_DAY)
    return f"D{d} {m // 60:02d}:{m % 60:02d}"


def parse_when(s: str) -> int:
    """'D3 14:00' or '3 14:00' -> sim minute."""
    day, hm = s.strip().split()
    h, mi = hm.split(":")
    return int(day.lstrip("Dd")) * MIN_PER_DAY + int(h) * 60 + int(mi)


def _db(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return db


def why(path: str, wid: str, when: str | None) -> str:
    """Why was workflow `wid` configured the way it was at `when`, who approved it, what happened next."""
    db = _db(path)
    t = parse_when(when) if when else 10 ** 9
    row = db.execute("SELECT version, actor, changed_dims, minute, proposal_id, approval_id FROM config_versions "
                     "WHERE wid=? AND (minute <= ? OR minute IS NULL) ORDER BY version DESC LIMIT 1",
                     (wid, t)).fetchone()
    if not row:
        return f"no config versions recorded for {wid}"
    ver, actor, dims, minute, pid, aid = row
    L = [f"# {wid} at {when or 'end'}: config v{ver}", "",
         f"- live since **{fmt(minute)}**, changed by **{actor}**, dims {dims}"]
    if pid:
        p = db.execute("SELECT route, state, body, intents, approval_id, submitted_minute, decided_minute, "
                       "applied_minute, reasons FROM proposals WHERE pid=?", (pid,)).fetchone()
        if p:
            route, state, body, intents, aid, sub, dec, app, reasons = p
            body = json.loads(body)
            L += [f"- proposal `{pid}` via **{route}**" + (f", approval `{aid}`" if aid else " (policy auto-apply)"),
                  f"- submitted {fmt(sub)}, decided {fmt(dec)}, applied {fmt(app)}; final state {state}",
                  "", "## What and why", ""]
            if body.get("change_summary"):
                L.append(f"**Summary:** {body['change_summary']}")
            L += [f"**Rationale:** {body.get('rationale')}", ""]
            if body.get("impact_score") is not None:
                L.append(f"**Impact score:** {body['impact_score']} · **affected leads:** {body.get('affected_leads')}")
            L += ["", "**Diff:**", "```", json.dumps(body.get("diff"), indent=1)[:3000], "```",
                  "**Dry run:**", "```", json.dumps(body.get("dry_run"), indent=1)[:3000], "```"]
            if route == "approval":
                L.append(f"Approved by the workflow's client approver (approval `{aid}`); reasons on decision: "
                         f"{reasons or '[]'}")
    elif actor == "supervisor":
        L.append("- a client supervisor edited this directly (outside governance); the controller did not revert it")
    nxt = db.execute("SELECT minute FROM config_versions WHERE wid=? AND version=?", (wid, ver + 1)).fetchone()
    until = nxt[0] if nxt else 10 ** 9
    L += ["", "## What happened next (decisions until the next version)", ""]
    for m_, kind, summ in db.execute("SELECT minute, kind, summary FROM decisions WHERE wid=? AND minute>=? AND "
                                     "minute<=? ORDER BY id LIMIT 40", (wid, minute or 0, min(until, (minute or 0) + 2 * MIN_PER_DAY))):
        L.append(f"- {fmt(m_)} `{kind}` {summ}")
    return "\n".join(L)


def customer(path: str, person_id: str) -> str:
    """What did this person hear, on which config version, and why was that version live (Compliance §3)."""
    db = _db(path)
    rows = db.execute("SELECT c.minute, c.ver, sw.s, sv.s, sc.s, sp.s, c.conn, c.eng, l.wid FROM calls c "
                      "JOIN strs sw ON sw.i=c.wm JOIN strs sv ON sv.i=c.vo JOIN strs sc ON sc.i=c.ci "
                      "JOIN strs sp ON sp.i=c.pv JOIN leads l ON l.lead_id=c.lead_id WHERE c.person_id=? "
                      "ORDER BY c.minute", (person_id,)).fetchall()
    if not rows:
        return f"no calls for {person_id}"
    L = [f"# Calls to {person_id}", "", "| when | config | welcome | voice | caller id | prompt | picked up | concluded |",
         "|---|---|---|---|---|---|---|---|"]
    for mi, ver, wm, vo, ci, pv, conn, eng, wid in rows:
        L.append(f"| {fmt(mi)} | v{ver} | {wm} | {vo} | {ci} | {pv} | {bool(conn)} | {bool(eng)} |")
    wid = rows[-1][8]
    L += ["", f"Run `python -m controller why --workflow {wid} --at \"{fmt(rows[-1][0])}\"` for why v{rows[-1][1]} was live."]
    return "\n".join(L)


def view(path: str, wid_filter: str | None = None) -> str:
    """One screen per workflow: what's tested, what's winning, what we want from you, what changed today."""
    db = _db(path)
    now = json.loads((db.execute("SELECT v FROM meta WHERE k='last_plan_minute'").fetchone() or ["0"])[0])
    L = [f"# Supervisor view — as of {fmt(now)}", ""]
    wids = [r[0] for r in db.execute("SELECT wid FROM workflows ORDER BY wid")]
    for wid in wids:
        if wid_filter and wid != wid_filter:
            continue
        spec = json.loads(db.execute("SELECT spec FROM workflows WHERE wid=?", (wid,)).fetchone()[0])
        L += [f"## {wid} ({spec['tenant_id']}) — primary {spec['objective']['primary']}, guardrail "
              f"{spec['objective'].get('guardrail')}", ""]
        cfg = db.execute("SELECT version, actor, config FROM config_versions WHERE wid=? AND config IS NOT NULL "
                         "ORDER BY version DESC LIMIT 1", (wid,)).fetchone()
        if cfg:
            c = json.loads(cfg[2])
            L.append(f"**Live config v{cfg[0]}** ({cfg[1]}): welcome {c['allocations']['default'].get('welcome_message')}"
                     f" · voice {c['allocations']['default'].get('voice')} · caller ID "
                     f"{c['allocations']['default'].get('caller_id')} · prompt {c['prompt_version']} · "
                     f"{len(c.get('cohorts', []))} cohorts")
        coh = db.execute("SELECT cid, where_json, owner FROM cohorts WHERE wid=? ORDER BY ord", (wid,)).fetchall()
        if coh:
            L.append("**Cohorts:** " + ", ".join(f"`{c}` {w} ({o})" for c, w, o in coh))
        L += ["", "**Being tested / winning:**", ""]
        exps = db.execute("SELECT dim, state, control, arms, champion, stats FROM experiments WHERE wid=? ORDER BY rowid",
                          (wid,)).fetchall()
        if not exps:
            L.append("- nothing (workflow's primary KPI is not moved by welcome/voice, or no test started yet)")
        for dim, state, control, arms, champ, stats in exps:
            s = json.loads(stats or "{}")
            latest = s.get("latest", {}).get("arms", {})
            arms_txt = ", ".join(f"{a}: {v['rel_lift']:+.1%} (n={v['n']}, p={v['p_better']})" for a, v in latest.items())
            L.append(f"- {dim}: **{state}** control {control}" + (f", champion **{champ}**" if champ else "") +
                     (f" — {arms_txt}" if arms_txt else ""))
        L += ["", "**Waiting for you:**", ""]
        pend = db.execute("SELECT pid, approval_id, body, submitted_minute FROM proposals WHERE wid=? AND state IN "
                          "('PENDING','APPROVED')", (wid,)).fetchall()
        if not pend:
            L.append("- nothing")
        for pid, aid, body, sub in pend:
            b = json.loads(body)
            L.append(f"- `{aid}` (impact {b.get('impact_score')}, {b.get('affected_leads')} leads): {b.get('change_summary')}")
        L += ["", "**Changes in the last 24h:**", ""]
        ch = db.execute("SELECT version, actor, changed_dims, minute FROM config_versions WHERE wid=? AND minute>=? "
                        "ORDER BY version", (wid, now - MIN_PER_DAY)).fetchall()
        if not ch:
            L.append("- none")
        for v, a, d, mi in ch:
            p = db.execute("SELECT body FROM proposals WHERE wid=? AND applied_version=?", (wid, v)).fetchone()
            summ = (json.loads(p[0]).get("change_summary") or json.loads(p[0]).get("rationale", "")[:120]) if p else ""
            L.append(f"- {fmt(mi)} v{v} by {a}: {d} {summ}")
        inc = db.execute("SELECT state, opened_minute, evidence, closed_minute FROM incidents WHERE wid=?",
                         (wid,)).fetchall()
        if inc:
            L += ["", "**Incidents:**", ""]
            for st, op, ev, cl in inc:
                e = json.loads(ev)
                L.append(f"- {fmt(op)} hallucination on {e.get('prompt')} ({e.get('rate')} vs {e.get('baseline')}) — "
                         f"{st}" + (f", resolved {fmt(cl)}" if cl else ""))
        flagged = db.execute("SELECT v FROM meta WHERE k=?", (f"did_flagged:{wid}",)).fetchone()
        if flagged and json.loads(flagged[0]):
            L.append(f"\n**Caller IDs flagged unhealthy:** {json.loads(flagged[0])}")
        L.append("")
    return "\n".join(L)


def status(path: str) -> str:
    """Loop health for the on-call engineer."""
    db = _db(path)
    L = ["# Controller status", ""]
    m = db.execute("SELECT COUNT(*), MIN(wall), MAX(wall), SUM(events), SUM(ingest_s), SUM(decide_s), SUM(advance_s), "
                   "MAX(rss_mb), MAX(minute), MIN(credits), MAX(lag_events) FROM metrics").fetchone()
    if m[0]:
        n, w0, w1, ev, ing, dec, adv, rss, minute, cr, lag = m
        L += [f"- sim time {fmt(minute)} · iterations {n} · wall {w1 - w0:.0f}s",
              f"- events ingested {ev} at {ev / max(ing, 1e-9):,.0f} events/s of ingest time",
              f"- time split: ingest {ing:.0f}s · decide {dec:.0f}s · simulator advance {adv:.0f}s",
              f"- peak RSS {rss:.0f} MB · credits remaining {cr} · max events behind at a decision {lag}"]
        per_day = db.execute("SELECT minute/1440 AS d, MAX(wall)-MIN(wall), SUM(events), SUM(decide_s) FROM metrics "
                             "GROUP BY d ORDER BY d").fetchall()
        L += ["", "| sim day | wall s | events | decide s |", "|---|---|---|---|"]
        L += [f"| D{d} | {w:.0f} | {e} | {ds:.1f} |" for d, w, e, ds in per_day]
    dec = db.execute("SELECT kind, COUNT(*) FROM decisions GROUP BY kind ORDER BY 2 DESC").fetchall()
    L += ["", "**Decisions by kind:** " + ", ".join(f"{k} {c}" for k, c in dec)]
    cred = db.execute("SELECT kind, COUNT(*), SUM(cost) FROM credits GROUP BY kind").fetchall()
    L += ["**Credit spend:** " + (", ".join(f"{k} ×{c} = {s:.0f}" for k, c, s in cred) or "none")]
    al = db.execute("SELECT minute, severity, wid, code, message, look_at FROM alerts ORDER BY id").fetchall()
    L += ["", "**Alerts** (page = wake someone; warn = next business day):", ""]
    L += [f"- {fmt(mi)} [{sev}] {wid} {code}: {msg} → look at: {look}" for mi, sev, wid, code, msg, look in al] or ["- none"]
    pr = db.execute("SELECT route, state, COUNT(*) FROM proposals GROUP BY route, state").fetchall()
    L += ["", "**Proposals:** " + ", ".join(f"{r}/{s} {c}" for r, s, c in pr)]
    return "\n".join(L)
