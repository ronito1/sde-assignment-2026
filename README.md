# Outcome-First Agent Loop

An always-on controller for outbound voice agents. For every workflow it watches the calls, decides what to
improve, gets the client's permission, applies the change, checks it worked, and records why. Nothing changes
on a client's agent without passing governance.

Original brief: [ASSIGNMENT.md](ASSIGNMENT.md) · Design and results: [SUBMISSION.md](SUBMISSION.md) ·
Runbook: [docs/CONTROLLER.md](docs/CONTROLLER.md) · Visual overview: [outcome-loop.html](outcome-loop.html)

## How it works, end to end

```
 ┌──────────────────────────────── SIMULATOR (the outside world) ────────────────────────────────┐
 │  leads · dialler · people who pick up or not · client approvers · supervisors · prompt checks │
 └──────▲───────────────────┬──────────────────────────────▲───────────────────────────▲─────────┘
        │ advance clock     │ call events                  │ change requests           │ validation
        │                   ▼                              │                           │
 ┌──────┴──────────────────────────────── CONTROLLER ─────┴───────────────────────────┴─────────┐
 │                                                                                               │
 │  1. INGEST      save new events + read position in one transaction (crash-safe)               │
 │        ▼                                                                                      │
 │  2. REACT       supervisor edited something? respect it; new prompt? safety-check it now      │
 │        ▼                                                                                      │
 │  3. APPLY       any request approved? apply it within its 2-hour window                       │
 │        ▼                                                                                      │
 │  4. THINK       four planners propose changes:                                                │
 │                   call time   → call each age group in its best hours        (pickup)         │
 │                   caller ID   → move off bad numbers, spread load            (pickup)         │
 │                   experiments → test welcome/voice, promote, roll back       (engagement)     │
 │                   incidents   → detect hallucination, validate a fix         (safety)         │
 │        ▼                                                                                      │
 │  5. GOVERN      bundle into one change per workflow:                                          │
 │                   operational + policy allows → auto-apply                                    │
 │                   customer-facing             → one approval request, in approver hours       │
 │        ▼                                                                                      │
 │  6. ADVANCE     move time 15–60 min, then repeat from 1 until the scenario ends               │
 │                                                                                               │
 │  every event, decision, request and result → one SQLite file (the audit trail)               │
 └───────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01 --force &
./run.sh http://127.0.0.1:8700          # runs until the scenario finishes
```

Run a whole scenario and grade it: `scripts/run_scenario.sh scenarios/public_02_incidents.yaml`. Run the tests:
`.venv/bin/python -m pytest -q tests`. Run the crash test: `scripts/crash_test.sh scenarios/public_02_incidents.yaml`.

## Results vs doing nothing

| Scenario | Leads concluded | Harmful changes |
|---|---|---|
| public_01_dev | +14.8% | 0 of 19 |
| public_02_incidents | +30.6% (bad prompt fixed in 150 min) | 0 of 31 |
| public_03_load (12 workflows) | +19.2% at ~31 s per simulated day | 0 of 54 |

**Stack:** Python 3, numpy, httpx, SQLite (WAL). One process, no extra services.
