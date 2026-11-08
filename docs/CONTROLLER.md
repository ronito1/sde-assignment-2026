# Controller runbook

For the engineer who owns this loop. The why behind each design choice is in `SUBMISSION.md`; this page is the
how.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt      # numpy, httpx (+ the simulator's deps)
.venv/bin/python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01 --force &
./run.sh http://127.0.0.1:8700                                           # runs until the scenario finishes
```

- `run.sh` uses `.venv` if it exists, and creates it if numpy/httpx are missing. There is no Docker and no
  service to start.
- State goes to `$CONTROLLER_DB` (default `state/controller.db`). This SQLite file is the only state.
- One scenario end to end (baseline, simulator, controller, grade, status, view): `scripts/run_scenario.sh
  scenarios/public_02_incidents.yaml 8700`. Output goes to `reports/<scenario>*.md`.
- Tests: `.venv/bin/python -m pytest -q tests` covers the simulator contract plus the controller units.
- Crash evidence: `scripts/crash_test.sh scenarios/public_02_incidents.yaml 8790 6`, with its log in `runs/crash_*.log`.

## Look at it (all read-only, safe while it runs)

| Command | Answers |
|---|---|
| `python -m controller status --db D` | Is the loop healthy? It shows ingest rate, events behind, wall time per sim day, credits, alerts and proposal states. |
| `python -m controller view --db D [--workflow W]` | The supervisor's screen: what's tested, what's winning, what we want from them, what changed in the last 24h, incidents and flagged caller IDs. |
| `python -m controller why --db D --workflow W --at "D3 14:00"` | Why W was configured like that at that time, who approved it, the evidence, and what happened next. |
| `python -m controller customer --db D --person W:p0001234` | What one person heard on each call, under which config version. Use it for an auditor's question. |
| `sqlite3 D "select * from decisions where wid='W' order by id"` | The raw decision log. Every detector firing, plan, submission, apply and verification is a row. |

## How it works, in one loop iteration (`controller/loop.py`)

1. **Ingest** (`model.py`). It fetches every chunk after the cursor and commits each chunk *with* the cursor in
   one transaction. Event ids at or below the cursor are skipped.
2. **React** to `config_changed` rows with `handled=0`. For a supervisor edit: refresh the config, adopt their
   cohorts, end any experiment on the dimension they touched, and validate a newly shipped prompt.
3. **Poll** in-flight approvals (`governance.poll`) and apply whatever is approved, within its 2h window.
4. **Plan** (`planners/`):
   - every iteration: incidents and caller-ID health;
   - hourly: call-time schedule and experiments.
5. **Flush** (`governance.flush`). Intents become at most one auto-apply and at most one approval request per
   workflow, after local preflight (`policy.py`), budget checks and approver-hours timing.
6. **Advance**: 15 min while anything is in flight, 60 min otherwise, and a jump overnight.

## The proposal outbox (why a crash cannot double-apply)

Every change is first committed as a `PREPARED` row in `proposals`, with a deterministic `proposal_id` and the
full body. Only then is the HTTP call made. On start-up, `governance.reconcile()` looks up each `PREPARED` id on
the server. If found, it adopts that result; if not, and the base version is unchanged, it resends; otherwise it
marks the row `STALE`. The server rejects a reused id, so a retry can never create a second request.
States: see the docstring at the top of `governance.py`.

## Alerts

Alerts live in the `alerts` table and are shown by `status`.

| Code | Severity | Means | Look at |
|---|---|---|---|
| `hallucination_incident` | page | A prompt is hallucinating at population level. A fix is being validated or proposed. | `why --workflow W`; the `incidents` table |
| `incident_fix_failed` | page | The applied fix did not verify (flags still high, or engagement collapsed). | `incidents.fix`; the next candidate is validated automatically |
| `approval_lapsed` | page | An approved change was not applied within its apply window. That is our fault. | Was the controller down? Check loop pacing |
| `credits_exhausted` | page | We can't validate a fix. | The `credits` table. Raise the budget, or a human picks the fix |
| `auto_apply_refused` | warn | The server's policy disagrees with our preflight. | `policy.py` against `/workflows/{id}` governance |
| `proposal_invalid` / `rejected_for_hygiene` | warn | A payload or budget bug. | `proposals.body` / `reasons` |

These deliberately do **not** alert: judgement-call rejections, expiries, inconclusive experiments and
caller-ID flags (they are handled automatically and appear in the view).

## Extending it

- **A new planner** implements `plan(m, wid, gov) -> [intent]`, `applied(...)` and `failed(...)`; see
  `planners/__init__.py` for the intent shape and helpers. Register it in `loop.Controller.__init__`. Its intents
  are batched with everyone else's automatically.
- **Tunables** live in `settings.py`, each with a one-line reason. Tenant agreements (e.g. Kestrel's 2
  requests/day) are in `TENANT_OVERRIDES`.
- **Rules:** never import `sim`, never read `runs/` or scenario files. The only source of truth is the HTTP API.

## Known sharp edges

- Restarting the **simulator** creates a new world. The controller detects this (a fingerprint or a smaller
  `last_event_id`) and wipes its store.
- Every call stays in memory (about 20 bytes each). That's fine for the scenarios, but not for months of
  production traffic; see SUBMISSION §8.
- Planners scan numpy arrays per workflow with a mask. That is O(total calls) per scan: fine at
  public_03_load, the first thing to index at 10×.
