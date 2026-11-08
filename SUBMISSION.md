# Outcome-First Agent Loop — Submission

**Name:** Ronit Pathak · **Date:** 2026-11-08 · **Hours actually spent (be honest):** ~6 h ·
**Language / storage:** Python 3.10+ (numpy, httpx) / one SQLite file in WAL mode (no new services)

> Runbook for whoever is on call: [`docs/CONTROLLER.md`](docs/CONTROLLER.md). Code: [`controller/`](controller/).

---

## 0. TL;DR

- An always-on controller drives the simulator over HTTP and owns three loops:
  - **call time and caller-ID health**, aimed at pickup;
  - **welcome × voice experiments with graduated decay rollback**, aimed at engagement;
  - **hallucination incident response**, using validation evidence and verification after the fix.

  All three go through one governance client (a proposal outbox, batching, approver-hours timing), and every
  decision lands in an audit store you can query (`python -m controller why …`).
- **public_01: conclusion rate +14.8% vs baseline, 0 harmful of 19 changes. public_02: +30.6%,
  0 harmful of 31.** Bad-prompt time-to-fix was 150 min using 6 credits, and the flagged caller ID
  was down to about 3% of traffic the next day. public_03_load (~829k events/day) took 31 s of wall
  time per simulated day, simulator included.
- Crash safety is demonstrated (`scripts/crash_test.sh`): 13 controller processes across fault injection and
  `kill -9`, all invariants pass, and the outcome is the same as a clean run.
- **Deliberately not built:**
  - per-person call time: the data is too sparse, and that was Product's "headline";
  - per-cohort variant allocation: the volumes have no statistical power for it;
  - cross-tenant fairness;
  - a UI: the supervisor view is a CLI or markdown screen.

## 1. Assumptions

1. **The guardrail `*_max_drop: 0.02` is absolute** (2 percentage points of the rate). At these volumes we
   cannot *prove* non-inferiority within 2 points, so a breach needs evidence: the drop beyond the allowance must
   reach z ≤ −2.58.
2. **Welcome and voice are heard after pickup**, so they cannot move pickup. On pickup-primary workflows (KYC) we
   don't experiment on them: only the guardrail would move, and there would be no upside on the primary KPI.
3. The approver decides at the **first working minute after a random latency** (docs §4.1). A request whose
   worst-case decision would land after its expiry is a certain "expired", so we never send it.
4. Pre-D0 history ran on the business default config with **no schedule table**, so it is a fair sample of
   hours: our call-time weights are learned from it.
5. The hallucination detector's false-positive rate is stable. The workflow's own history flag rate (≈1.4–1.5%)
   is the baseline, and the detector's noise is already inside it.
6. **Caller IDs wear out with load.** We observed this: a number that carried the traffic for a week answers
   worse than a fresh one in the same hours. Spreading traffic is the default, and "flagged" is reserved for a
   sharp drop.
7. A **supervisor edit is the new status quo.** We build later proposals on top of it and never revert it
   silently. A revert is offered only through an approval, with evidence (the incident path).
8. Kestrel's "2 requests a day, never before 11:00" is a **voluntary limit stricter than policy** (4/day,
   10:00). An *incident fix* may still use a reserved slot and go out from 10:00.
9. Credits are spent only where they change a decision: validating a newly shipped prompt, and validating fix
   candidates. Routine work spends none.
10. The only source of truth is the HTTP API. If the simulator restarts, it is a new world, and the store is
    wiped (detected by fingerprint and event id).
11. "Named human approval" (Compliance) is the workflow's approver behind `/approvals`. We record the
    `approval_id` and decision time; the API doesn't expose names.
12. SQLite is the store for the take-home; production uses the existing Postgres, with the same schema (§8).

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | **Built** | Everything else depends on it: chunk + cursor in one transaction, event-id dedupe, restartable. Measured at load scale. | Streaming parse for 10× (§8) |
| C2 | Shared cohort object | **Built** | One registry (`cohorts.py`) used by config, schedule, analysis, view and audit. Supervisor cohorts are adopted, never deleted. | — |
| C3 | Bootstrapping from history | **Partial** | History gives hour × cohort pickup, per-DID baselines and the hallucination baseline. It cannot give variant priors: history has no variation. | Nothing useful left to take from it |
| C4 | Call-time optimisation | **Partial** (per cohort) | Per-cohort hour weights from history, batched into the first approval. Per person is cut: 20–40% of people return, with a handful of attempts each. | A per-person posterior over hour for returning people, capped at 200k entries |
| C5 | Caller-ID health | **Built** | Population test against peers in the same hours, a 3% probe, graduation on a lower confidence bound, a 70% cap, and spreading against wear. | Model wear explicitly (load → health) |
| C6 | Variant experimentation | **Built** | Factorial welcome × voice at a 30% exploration cap, Holm across arms plus alpha spread over looks, minimum sample sizes. | Per-cohort arms (needs about 5× the volume, §5) |
| C7 | Promotion, decay, rollback | **Built** | Promote with a 15% holdout. Two decay signals. Graduated rollback: 50/50 to confirm, then a full rollback. | Faster decay detection (§13) |
| C8 | Hallucination response | **Built** | Validate a shipped prompt immediately; otherwise use a binomial tail on live flags. Fix candidates cheapest first; validated; proposed with evidence; verified on flags *and* engagement. | — |
| C9 | Governance client | **Built** | Outbox, state machine, one request in flight per workflow, batching, approver-hours timing, fatigue budget with a reserved incident slot, re-proposal on expired/stale/lapsed, auto-apply only within policy and after preflight. | — |
| C10 | Honest impact, affected leads, dry run | **Partial** | Dry runs replay measured rates. Impact = expected extra conclusions/day scaled to [0,1] × confidence. | A calibration report: predicted vs realised impact |
| C11 | Supervisor view | **Partial** (CLI/markdown) | `python -m controller view`: one screen per workflow. | A web page over the same queries |
| C12 | Audit store | **Built** | Every decision is a row. `why` and `customer` answer the compliance questions from our store alone. | — |
| C13 | Crash recovery | **Built** | `scripts/crash_test.sh` plus `verify_run.py` invariants. | — |
| C14 | Loop observability | **Partial** | A `metrics` table, `status`, and an `alerts` table split into page and warn. | Export to their Prometheus |
| C15 | Fairness across tenants | **Cut** | Approver attention is already per workflow (policy budgets); credits are spent only on incidents. | A per-tenant credit quota in `settings` |

**The principle:** first, anything without which the loop is *unsafe* (C1, C9, C12, C13, and the incident path
C8). Then the changes with the biggest measured outcome per unit of risk. Call time and caller-ID spreading were
worth +25–30% pickup on KYC, and they are operational changes that the policies allow. Variant experiments come
next, and only where the statistics can work at the observed volume. Wherever the data could not support
something (per-person timing, per-cohort arms), we said no rather than ship a sketch.

## 3. Stakeholders

Every claim was checked against `GET /workflows/{id}` (`governance`, `objective`, `options`):

| | nw_renewal | nw_kyc | ks_cancel |
|---|---|---|---|
| primary / guardrail | engagement / pickup −0.02 | **pickup** / engagement −0.02 | engagement / pickup −0.02 |
| auto-apply | schedule, caller_id; max_shift 0.5 | **disabled** | schedule, caller_id, welcome, voice; **max_shift 0.2** |
| approver hours / latency / requests per 24h | 10–19 / 30–240 min / 3 | 10–18 / **60–480** min / 2 | 10–19 / 20–120 min / 4 |
| mid-flight | allowed | **next_campaign_only** | allowed |
| prompts offered | P1 P3 P4 P5 | **P1 P3** | P1 P3 P4 (+P2 in public_02) |

### 01 — Sales (Nikhil)
- **Accepted:**
  - speed where it is free: winners are proposed at the first look that is significant;
  - batched requests, so supervisors aren't flooded;
  - a per-client lift figure: `reports/` has it per workflow, for each KPI.
- **Declined:**
  - a blended score (constraint 3, Product);
  - "live for everyone by evening": a winner needs about 250 connects per arm and an alpha-corrected test, then
    approver latency and the apply window;
  - pushing "persuasive" everywhere: prompts change per workflow, only with validation evidence, and an LLM
    never decides.
- **Wrong:**
  - "Kestrel turned on auto-apply for everything": no prompt changes, and any auto step moves at most 20%
    of traffic.
  - "Northwind is fine with auto-apply on KYC": nw_kyc has auto-apply **disabled**.
  - The persuasive prompt isn't even offered on KYC.
- **Reply:** *"Nikhil — you'll have a per-client lift number for the summit, one per KPI (pickup and
  engagement), straight from the grader's comparison with no-op runs. On the dev scenarios conclusions rose
  15–30%. Two corrections before anyone says it on stage: Kestrel's auto-apply covers timing, caller ID and small
  welcome/voice shifts, not prompts, and Northwind KYC has auto-apply switched off entirely. 'Improved itself
  overnight' is true for call timing. Variant winners take two to three days of calls to prove, and we'd rather
  demo a winner a compliance head can sign than one we have to roll back on stage."*

### 02 — Northwind Compliance (Meera)
- **Accepted:**
  - every customer-facing change (welcome, voice, prompt) goes through a named approval, never auto-apply,
    including on workflows whose policy would allow it;
  - prompt fixes carry a `validation_run_id`;
  - `python -m controller customer --person …` answers "what did this person hear, and why was it live" from
    our own store, in seconds;
  - "slow and right": small alphas, a 30% cap on exploration, graduated rollback.
- **Pushed back:** exploration means some customers hear an untested variant. We cap it at 30% of calls per
  dimension and drop a worse arm as soon as it is significantly worse, but "never worse for any cohort, even
  briefly" is not achievable for anyone who experiments.
- **Wrong:** "call timing and caller-ID rotation are covered by the policy we signed" is true for **renewal**
  only. **KYC's policy disables auto-apply**, so even timing and caller-ID changes on KYC go to your approvers.
  Your approver hours (10–19, KYC 10–18) match the contract.
- **Reply:** *"Meera — agreed on all five points, with one correction in your favour. The KYC policy you signed
  has auto-apply switched off, so every KYC change, including call timing and caller ID, comes to your
  approvers. Renewal timing and caller-ID changes do auto-apply within policy, and they are listed in the weekly
  summary with the reason. Script changes always carry a validation run id. For an IRDAI question we can show,
  per customer and per call, the variant they heard, the config version, who approved it and the evidence,
  within minutes. Requests go out only when your approvers can decide them the same day."*

### 03 — Platform/SRE (Kabir)
- **Accepted:**
  - kill/restart is tested, not promised (§9);
  - idempotency through the proposal outbox and an event-id cursor;
  - page vs warn alerts, each with "look at";
  - loop health without logs (`status`);
  - the 10-minute/day budget (measured: 31 s/day).
- **Pushed back:** SQLite instead of Postgres for the take-home. It is a file, not a service, so there is
  nothing to operate at 2 a.m., and the schema is plain SQL that ports to the existing Postgres for production.
- **Wrong:** nothing; the memo's demands match the contract.
- **Reply:** *"Kabir — no new stateful infra: state is one SQLite file locally, and the same tables move to your
  Postgres in production. The kill test is scripts/crash_test.sh: 13 processes, including faults injected between
  writing a proposal and sending it, with checks against the server for no double-apply and no lost approval.
  The load scenario runs at 31 s per simulated day on a laptop, simulator included. The first things to
  break at 10× are the in-memory call log and single-process JSON parsing; the fix is partitioning by tenant.
  Pages only for: hallucination incident, failed fix, lapsed approval, credits exhausted."*

### 04 — Kestrel ops supervisor (Anita)
- **Accepted:**
  - at most 2 requests per 24h and none before 11:00 (stricter than the policy's 4 and 10:00);
  - one batched request carrying all changes;
  - her edits are never reverted behind her back: we end our experiment and treat her choice as the new control;
  - one screen (`view`);
  - fixes are verified on engagement as well as hallucination, so a "word-for-word" fix that kills engagement
    would reopen the incident.
- **Pushed back:**
  - "Tell me before anything changes": the policy lets us auto-apply caller-ID and timing changes, and
    *safety* moves (dropping a losing variant, rolling back a decayed one) on a running campaign. We do, and we
    show them in her view. Promotions of new variants always come to her as a request.
  - When a prompt she ships is unsafe (public_02: P2 failed validation at 0.937), we propose the revert as an
    incident request, with evidence. That is still her approver's call.
- **Wrong:** "two requests a day max" is her preference, not the contract (4 per 24h). We honour it anyway.
- **Reply:** *"Anita — your rules are in the controller: at most two requests a day, nothing before 11:00, and
  each request bundles everything. Your prompt edits stand. If one is unsafe, we validate it within the hour and
  send you a request to switch back, with the evidence. We never switch it ourselves. Small operational moves
  (caller ID, call timing, and pulling a variant that is losing) apply automatically, as your policy allows, and
  appear on your screen under 'changes in the last 24h'. Fixes are checked for engagement too, so no
  verbatim-script surprises."*

### 05 — Product (Tara)
- **Accepted:**
  - separate KPIs everywhere;
  - one cohort object;
  - the minimum-size reasoning (§5);
  - the proposal payload with an impact score that means something (§6);
  - **expired = rejected**, re-planned from fresh data under a new id;
  - evidence only from the `variant` recorded on each call (an arm that never ran has no data);
  - winners scoped to their workflow.
- **Declined:** per-person call time as a P0. It is cut, with the reason: about 70% of people are seen once,
  with up to 8 attempts, which is too sparse to learn a personal hour. Per-cohort timing captures the
  age-driven pattern now. Caller-ID health and the supervisor view are built.
- **Wrong:** the plan assumed per-person timing has the data to be a headline; it doesn't.
- **Reply:** *"Tara — your definitions are now constraints in code: KPIs never blended, one cohort registry,
  expired treated as rejected and re-planned, only executed arms count as evidence. Minimum size: no decision
  under 250 connects per arm, alpha 0.05 spread over six looks with Holm across arms. That detects about a 15%
  relative lift in 2–3 days at dev volume and misses 5% lifts (§5 has the table). I cut per-person call time:
  most people are seen once, too sparse to learn a personal hour. Per-cohort timing ships instead, and it
  produced the largest gains we measured."*

### 06 — CTO (Aryamann)
- **Accepted, all of it:**
  - explainability (audit), with harm treated as worse than nothing (0 harmful changes was the bar we tuned to);
  - compute only where it changes a decision (6 credits for the whole of public_02);
  - scale numbers;
  - a written cut list;
  - docs for on-call.
- **Wrong in the memos, found by "trusting the contract":** Kestrel isn't "auto-apply for everything"; KYC
  auto-apply is off; KYC is pickup-primary (the example controller optimised its engagement); and Compliance
  thinks KYC timing changes are auto.
- **Reply:** *"Aryamann — five things done properly: governance, call time and caller ID, experiments with
  rollback, incident response, and audit with crash safety. I said no to per-person timing, per-cohort arms and
  cross-tenant fairness. Each 'no' is written down with what it would take. Zero harmful changes on public_01,
  public_02 and a perturbed copy. What breaks first at 10× is in §8. The weakest part is decay detection at low
  volume, and §13 says why."*

## 4. Architecture

```
            ┌──────────────────────────────── simulator (HTTP only) ─────────────────────────────────┐
            │ /sim/advance  /events/chunks  /workflows  /approvals  /workflows/{id}/apply|config  ... │
            └───────▲──────────────┬────────────────────────────────▲──────────────────▲────────────┘
                    │ advance      │ NDJSON chunks                  │ submit/poll/apply │ validate/LLM
┌───────────────────┴──────────────▼────────────────────────────────┴──────────────────┴───────────┐
│ loop.py   ingest → react to config changes → poll/apply → plan → flush → metrics → advance      │
│                                                                                                   │
│  model.py ── ingest: chunk + cursor in ONE tx; lead features joined in memory; CallLog (numpy)     │
│  cohorts.py ─ shared cohort registry (chosen from history; supervisor cohorts adopted)             │
│  planners/  schedule (C4) · caller_id (C5) · experiments (C6/C7) · incidents (C8) → intents         │
│  governance.py  intents → compose on current config → preflight (policy.py) → route:              │
│                 auto (policy-permitted)  |  approval (outbox: PREPARED → PENDING → … → APPLIED)     │
│  report.py  why / customer / view / status (read-only)                                             │
└───────────────────────────────────────────────┬───────────────────────────────────────────────────┘
                                                │ every write in a transaction
                     ┌──────────────────────────▼──────────────────────────────┐
                     │ SQLite (WAL): meta(cursor) · calls · leads · campaigns · │
                     │ config_versions · cohorts · proposals(outbox) ·          │
                     │ experiments · incidents · credits · decisions · alerts · │
                     │ metrics                                                  │
                     └──────────────────────────────────────────────────────────┘
```

**On restart:**
1. Check the world fingerprint.
2. Reload calls into numpy, plus the live leads.
3. Fetch the chunks after the cursor.
4. `governance.reconcile()` settles every `PREPARED`/`PENDING`/`APPROVED` row against the server, by
   `proposal_id`.
5. Handle `config_versions` rows not yet marked handled.
6. Continue from the simulator's current minute.

Nothing in memory is authoritative.

## 5. The learning loop

**Cohorts.** For each lead feature, a chi-square test of value × hour interaction on pickup, computed from
history. The feature with the strongest interaction (z > 4) defines the cohorts: one per value carrying ≥ 10% of
dials, at most 4, and everything else is `default`. All four public workflows picked `age_band`, which is the
time-of-day archetype we expected. Cohorts are deliberately coarse: an experiment arm inside a 15% cohort would
see only about 15 connects a day at dev volume.

**Minimum sizes and evaluation window** (engagement around 0.55, α 0.05 two-sided, power 0.8, per arm):

| relative lift | 3% | 5% | 10% | 15% | 20% |
|---|---|---|---|---|---|
| connects per arm | 14,216 | 5,102 | 1,263 | 554 | 307 |

At dev volume (~1,000 connects a day per workflow), a 10–15% challenger arm reaches 250 connects in about 2 days.
So the rules are: **no decision below 250 connects per arm** (minimum detectable ≈ 20% at the first look,
≈ 10–15% by day 4), and **one winner look per day, at most 6 looks.** The cost: 3–5% effects are invisible at dev
volume (at load volume, about 15k connects a day, they become visible in days).

**Experiment design.**
- Applies to engagement-primary workflows only.
- Welcome and voice launch together. The simulator samples each dial's welcome and voice independently, so it
  is a factorial: every connect is evidence on both margins.
- Control keeps 70%; challengers share 30%. This cap bounds the worst-case harm of a bad challenger set (30% ×
  −3% = −0.9%, under the 1% harm line).
- Allocation stays fixed between looks (no bandit), because every reallocation costs a governance request and
  would break the test's assumptions.
- Interactions: not modelled. The margins are unbiased under independent randomisation. A real welcome × voice
  interaction would show up as a winner that underperforms after promotion, which the decay watch catches.

**Deciding a winner.**
- One-sided two-proportion test against control. α = 0.05 is split evenly over 6 looks (0.0083 per look), and
  Holm is applied across arms.
- Also required:
  - ≥ 2% relative lift (point estimate);
  - still ahead of control over the last 24h;
  - no significant guardrail breach.
- **False-winner rate** ≤ 5% per experiment under the null (Bonferroni over looks, Holm over arms), lower in
  practice because of the extra conditions.
- **Drop rule** (every 6h): an arm significantly worse than control (one-sided p < 0.01, ≥ 250 connects) loses
  its traffic to control. It's loose because dropping is cheap and safe.
- Deterministic: same data, same decision. The LLM never takes part.

**Decay and rollback.**
- Promotion keeps a **15% holdout** on the old control. Two signals are checked every 6h:
  - **(a) champion vs holdout** over the trailing 24h, p < 0.01, or a significant guardrail breach;
  - **(b) the champion's own drift**: its last 24h compared with the lower 80% bound of its experiment-period
    rate (to discount winner's curse), at p < 0.005. Vetoed if the holdout fell as much (a base shift such as
    a prompt change), and only acted on once the champion is no better than the holdout over the same 24h.
- The response is **graduated**:
  1. demote to 50/50, which halves exposure and gives the follow-up test real power;
  2. when the champion is no better than the holdout at 50/50, roll back fully and ban the champion from
     re-exploration;
  3. if instead it is clearly ahead after a day, re-promote it.

  Speed: on public_01 the decaying W3 went from 85% of traffic to 0% between day 4 and day 6 after decay onset,
  with no harmful step. A one-step rollback was harmful by 1.0–1.4% in earlier iterations, because it landed
  while W3 was still marginally better.

**Call time and caller ID.**
- *Call time:* per-cohort hour weights from history. The pickup profile is shrunk toward the pooled hour effect,
  squared, and clipped to [0.3, 3]; every hour in the window keeps a positive weight. The weights go into the
  first daily request, together with the cohort definitions, which always need approval.
- *Caller ID:* hour-stratified pickup ratio against peers.
  - Flag at ratio < 0.7 with z < −4 over ≥ 250 dials in 6h. Traffic moves off at once, keeping a 3% probe.
  - Untested numbers get a 3% probe, and graduate only when the lower 90% bound of their ratio is ≥ 0.9.
  - Healthy numbers share traffic almost equally, with no number above 70%.
  - What we learned the hard way: with ratio⁴ weighting and a looser flag, wear made one number look bad, its
    traffic moved to the next, which wore out in turn, and the chain ended on the weak D4.

**Guardrail.** Each candidate winner is checked against control on the guardrail KPI. Promoted champions are
checked continuously against the holdout. A breach (z ≤ −2.58 beyond the allowance) blocks promotion, or starts
a rollback.

## 6. Governance

```
 approval:  PREPARED ─submit─▶ PENDING ─▶ APPROVED ─apply─▶ APPLIED
               │                 │  │        ├─409 base moved─▶ STALE  ─▶ re-plan on fresh config (new id)
               │                 │  │        └─410 too late ───▶ LAPSED ─▶ page + re-plan
               │                 │  ├─▶ REJECTED ─▶ same idea backs off 12h
               │                 │  └─▶ EXPIRED  ─▶ treated as rejection: re-plan from fresh data
               └─409 stale / 422 ─▶ STALE | INVALID
 auto:      PREPARED ─PUT─▶ APPLIED | REFUSED(403, alert) | STALE(409)
```

- **Batching.** One approval request in flight per workflow. Every planner's intents that need approval are
  composed into it, on top of the current config. Example from public_01: cohorts + call time + welcome test +
  voice test went as one request. An incident fix may overtake a routine request in flight. While any approval
  is in flight, only incident-level auto-applies proceed, so the request isn't made stale for nothing.
- **Timing.** The submit rules:
  - not before approver start (11:00 for Kestrel);
  - routine requests only if a same-day decision is possible (`now + max latency ≤ approver end`);
  - for workflows where same-day is never possible (nw_kyc: 60–480 min latency), the first hour of the day;
  - never if the worst-case decision would land after expiry.

  We poll every 15 min while anything is pending, so approved changes are applied well inside the 2h window.
- **Fatigue.** Budget = min(policy, tenant override) per rolling 24h, counting every request created, rejected
  and expired included, with 1 slot kept for incidents. Across all public runs: **0 expired, 0 lapsed, 0 fatigue
  rejections, peak ≤ 2/24h.**
- **Auto-apply.** Used for operational dimensions (schedule, caller ID) and for **safety** moves (dropping a
  losing arm, demoting or rolling back a decayed champion), always after the local preflight. In each step it
  moves at most `max_shift`. It is **voluntarily not** used for launching tests or promoting winners, even on
  Kestrel where policy permits it: those are customer-facing bets, and a human should see them first.
- **Payload honesty.**
  - `affected_leads`: live leads in the active campaigns plus the next batch (for next-campaign-only workflows,
    the next batch only), restricted to the affected cohorts.
  - `dry_run`: measured arm rates replayed on the last day's connects (experiments); history replayed with the
    proposed hour weights (schedule); hour-stratified DID ratios applied to the new split (caller ID); validation
    plus the candidate's live flag record (prompts).
  - `impact_score`: min(1, expected relative gain ÷ 10%) × (1 − p). Combined across a batch as 1 − Π(1 − iᵢ).
    It ranks the queue sensibly, but it isn't calibrated against realised impact yet (§13).
- **Supervisor edit mid-experiment** (public_02, nw_renewal at D5 15:00):
  - the experiment on that dimension ends, and the supervisor's choice becomes the control;
  - our in-flight rollback was then refused (stale), and we re-proposed it on top of her config the same day;
  - nothing of theirs was reverted.

## 7. Incidents

| Detector | Threshold | Expected false positives |
|---|---|---|
| Newly shipped prompt (supervisor edit) with no clean live record | one validation run (n=300, 3 credits); pass rate < the policy minimum opens an incident | validation SE ≈ 0.6pt; a ~0.99 prompt fails 0.97 under 1% of the time |
| Live flags since a prompt change | ≥ 150 connects, binomial tail p < 1e-3 and ≥ 1.8× baseline | ≈ 1e-3 per look; few looks per change |
| Live flags with no change to suspect | rolling 800 connects, p < 1e-6 and ≥ 1.8× | ≈ 1e-6 per look. At p < 1e-3 we *did* get one false alarm in the load test, across ~12 workflows × hundreds of looks |
| Caller ID | ratio < 0.7, z < −4, ≥ 250 dials in 6h | z < −4 is ~3e-5 per DID per look |

**Picking a fix, cheapest first:**
1. The prompt that ran before the suspect, if its own live flag rate is normal. It is the only candidate with
   *engagement* evidence.
2. Other prompts with a clean live record.
3. Only then `/llm/suggest-fixes` (3 credits).

Each candidate is validated (3 credits) before it can be proposed. **Verification after apply:** ≥ 150 connects
on the new prompt, with a flag rate not significantly above baseline *and* engagement not more than 5% below the
pre-incident level at p < 0.01. Otherwise the incident reopens with the next candidate.

**public_02 timeline** (from `decisions`):

| sim time | what |
|---|---|
| D2 10:00 | ks_cancel D2 spam-flagged (hidden) |
| D2 12:15 | `did_flagged` D2 at 64% of peers over 396 dials (z = −4.2); moved to 12.5%, then to the 3% probe at 13:00 (two auto steps, max_shift 0.2). Flagged-DID share: day 2 11%, day 3 onward about 3% |
| D3 11:30 | supervisor ships prompt P2 (`supervisor_edit`, not reverted) |
| D3 12:00 | `validation_run` P2: pass rate 0.937 < 0.97 → `incident_opened`; `validation_run` P1: 0.990 → `proposal_submitted` "switch prompt P2 → P1", with the validation id, flagged as reversing a supervisor edit |
| D3 14:00 | approved and `applied` (v9); `incident_fix_applied` → verifying |
| later | `incident_resolved`: flags back to baseline, engagement unchanged |

**Time-to-fix: 150 min** (it was 285 min before we started validating shipped prompts). **Credits: 6.**

## 8. Scale

Measured on `public_03_load` (12 workflows, 5 days, 7 days of history), on a MacBook, caffeinated, with nothing
else running:

| metric | value |
|---|---|
| history ingest at boot | 5.5M events in ~45 s (≈ 120k events/s, parse + SQLite + numpy) |
| steady-state ingest | 113,077 events/s of ingest time |
| wall time per simulated day (controller + simulator) | 31 s (SRE budget: 600 s) |
| time split over the run | per full day: ingest 7 s · decide 17 s · simulator advance 7 s |
| decision lag | 0 events behind at every decision; decisions every 15–60 sim-min |
| peak RSS | 1727 MB |

**What breaks first at 10×** (6.5M dials/day, 8.5M events/day, hundreds of workflows):
1. **The in-memory call log.** Every call is kept, about 20 bytes each plus Python overhead: 65M calls a day
   would exhaust 8 GB within a week. Fix: keep raw calls only for a rolling window (about 3 days) and
   aggregate older data into (workflow, day, hour, cohort, variant) counts. Every statistic we compute is a
   roll-up of those counts.
2. **Single-process JSON parsing** at ~100–120k events/s: about 85 s of parsing per 10× day. That still fits,
   but it is the CPU ceiling. Fix: parallel parse per chunk, or orjson.
3. **Planners scan with a full-array mask**, which is O(all calls) per scan. Fix: per-workflow index arrays
   (append-only), which removes most of the decide time.
4. **SQLite as a single writer.** Production moves to Postgres with `COPY` into partitioned `calls` tables, or
   drops raw-call storage for aggregates plus the raw NDJSON in object storage for audit.

**Partitioning.** Workflows are independent except for tenant-level things (credits, approver budgets, the
Kestrel override). So: **shard by tenant**, one worker per shard, all against the shared Postgres, with an
advisory lock per tenant so two workers never own the same tenant. The outbox makes failover safe: a new owner
reconciles by `proposal_id`.

## 9. Reliability

**Crash test** (`scripts/crash_test.sh scenarios/public_02_incidents.yaml`; log in `reports/crash_test.log`):
- 3 runs with `CONTROLLER_CRASH_AT=after_prepare:n`, which kills the process after a proposal row is committed
  and before it is sent, the worst moment for duplicates;
- 1 run killed just before `/sim/advance`;
- 8 random `kill -9`s, several of them during boot or reconcile;
- then a final run to completion. **13 controller processes in total.**

`verify_run.py` then checks against the **server**:
- no proposal applied twice;
- every applied change is in our store with the right version;
- no approval on the server is missing from our store;
- every server-applied approval is `APPLIED` for us;
- no `PREPARED` row is left over.

**All pass.** The outcome matches a clean run within noise (+29.9% vs 30.6%). **State lost:** none
beyond the in-flight iteration, which is recomputed from durable state.

**Idempotency keys:**
- `event_id` cursor plus a chunk-level transaction (ingest);
- `proposal_id = <scenario>:<workflow>:<route>:<durable sequence>`, written before sending (governance);
- `approval_id` from the server;
- `config_versions(wid, version)` as the primary key.

**On simulator errors:**

| response | behaviour |
|---|---|
| transport error or 5xx | retried with backoff (8×). `/sim/advance` is *never* retried blindly; we re-read `/sim/state` |
| 409 `duplicate_proposal_id` | treated as "already sent"; reconcile through the approvals list or config history |
| 409 `stale_base_version` | STALE; refresh the config and re-plan on top of it |
| 410 `approval_lapsed` | LAPSED; page (our fault); re-plan |
| 409 `not_approved` with status applied | marked APPLIED (we crashed after apply) |
| 403 `auto_apply_not_permitted` | prevented by preflight; if it happens anyway, REFUSED plus an alert |
| 422 `invalid_config` / `no_change` | prevented by `policy.validate`; `no_change` means someone already did it |
| 402 `out_of_credits` | page; the deterministic loop keeps running without LLM or validation |
| 409 `finished` | clean exit |

## 10. Audit & observability

```
$ python -m controller why --db state/public_02_incidents.db --workflow ks_cancel --at "D3 15:00"
```
```text
# ks_cancel at D3 15:00: config v9

- live since **D3 14:00**, changed by **controller_approved**, dims ["prompt_version"]
- proposal `public_02_incidents:ks_cancel:approval:00016` via **approval**, approval `apr_00007`
- submitted D3 12:00, decided D3 13:36, applied D3 14:00; final state APPLIED

## What and why

**Summary:** Fix hallucinations: switch prompt P2 → P1
**Rationale:** P2 (live since D3 11:30) failed validation: pass rate 0.9367 on 300 synthetic conversations (val_00001). P1 passed validation at 0.990 on 300 conversations and has a normal live flag record (551/36954). This reverses a supervisor's prompt edit; it is proposed, never forced.

**Impact score:** 0.95 · **affected leads:** 4188

**Diff:** `{"changed_dims":["prompt_version"],"incident":{"prompt_version":{"from":"P2","to":"P1"}}}`

**Dry run:** `{"incident":{"candidate_live_flags":{"connects":36954,"flags":551},"incident":{"baseline":0.0153,"connects":43,"flags":0,"p_value":null,"prompt":"P2","rate":0.0633,"since":"D3 11:30","source":"validation of a newly shipped prompt","validation":{"credits_remaining":197.0,"failures":19,"n":300,"pass_rate":0.9367,"prompt_version":"P2","ts":"2027-01-07T12:00:00+05:30","validation_run_id":"val_00001","workflow_id":"ks_cancel"}},"method":"validation run + live flag history of the candidate prompt","validation":{"credits_remaining":194.0,"failures":3,"n":300,"pass_rate":0.99,"prompt_version":"P1","ts…`
Approved by the workflow's client approver (approval `apr_00007`); reasons on decision: []

## What happened next (decisions until the next version)

- D3 14:00 `applied` public_02_incidents:ks_cancel:approval:00016 applied as v9 (controller_approved)
- D3 14:00 `incident_fix_applied` P1 live in v9; verifying
- D3 15:30 `incident_resolved` P1 verified: flags 2/151=1.3%, engagement +0.5% vs pre-incident
- D3 18:00 `applied` public_02_incidents:ks_cancel:auto:00017 applied as v10 (controller_auto)
- D3 18:00 `experiment_arm_dropped` voice: dropped ['V4'] (v10)
```
{
 "changed_dims": [
  "prompt_version"
 ],
 "incident": {
  "prompt_version": {
   "from": "P2",
   "to": "P1"
  }
 }
}
```
**Dry run:**
```
{
 "incident": {
  "candidate_live_flags": {
   "connects": 36954,
   "flags": 551
  },
  "incident": {
   "baseline": 0.0153,
   "connects": 43,
   "flags": 0,
   "p_value": null,
   "prompt": "P2",
   "rate": 0.0633,
   "since": "D3 11:30",
   "source": "validation of a newly shipped prompt",
   "validation": {
```

For one customer: `python -m controller customer --person <id>` lists every call with welcome, voice, caller
ID, prompt and config version.

**Page on (wake someone):**
- `hallucination_incident`: customers are hearing it now;
- `incident_fix_failed`;
- `approval_lapsed`: a human approved and we failed to act;
- `credits_exhausted` during an incident.

**Don't page (next business day or the view):**
- judgement rejections and expiries (handled: re-planned);
- caller-ID flags (handled automatically, within policy);
- inconclusive experiments;
- `auto_apply_refused` and `proposal_invalid` (warn: a preflight or payload bug, no customer impact).

## 11. Results

### public_01_dev
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.399 | +16.8% | 0.264 | +7.7% | 0.528 | +7.4% | 33% | 70 (57) |
| nw_kyc | 0.454 | +23.9% | 0.302 | +24.7% | 0.584 | -2.2% | 0% | 34 (21) |
| nw_renewal | 0.448 | +7.6% | 0.276 | +4.6% | 0.554 | +2.3% | 17% | 48 (54) |
| ALL | 0.427 | +14.8% | 0.275 | +9.7% | 0.549 | +3.7% | 19% | 152 (132) |

Changes applied: **19** (neutral: 6, beneficial: 13). Governance: 11 requests, decisions {'approved': 10, 'rejected': 1, 'expired': 0}, applied via approval 10, auto-applied 9, auto-apply refused 0, apply conflicts 0, lapsed 0, peak requests/24h {'nw_renewal': 2, 'nw_kyc': 1, 'ks_cancel': 1}. Credits used: 0 · validation runs: 0 · LLM fix calls: 0

### public_02_incidents
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.373 | +58.4% | 0.248 | +37.1% | 0.512 | +9.5% | 42% | 87 (348) |
| nw_kyc | 0.476 | +27.3% | 0.301 | +28.6% | 0.593 | -2.5% | 0% | 45 (29) |
| nw_renewal | 0.484 | +11.8% | 0.293 | +11.7% | 0.554 | -0.1% | 8% | 64 (54) |
| ALL | 0.432 | +30.6% | 0.274 | +24.8% | 0.545 | +2.3% | 26% | 196 (431) |

Changes applied: **31** (neutral: 16, beneficial: 15). Governance: 15 requests, decisions {'approved': 13, 'rejected': 2, 'expired': 0}, applied via approval 12, auto-applied 19, auto-apply refused 0, apply conflicts 1, lapsed 0, peak requests/24h {'nw_renewal': 2, 'nw_kyc': 1, 'ks_cancel': 1}. Credits used: 6 · validation runs: 2 · LLM fix calls: 0

**Interpretation.**
- **Where we helped most: pickup on every workflow** (+5–40%). Call time per age cohort plus caller-ID spreading
  did it; on public_02 also moving off the flagged number. On ks_cancel in public_02 the large figure is mostly
  incident avoidance: the baseline stays on the flagged DID and the bad prompt.
- **Engagement**: +4–9% on ks_cancel (the V4 or W2 winners, dropping the W3 loser), +1–3% on nw_renewal (W3 while
  it worked, then a rollback).
- **Harmful: none applied.** Earlier iterations were harmful in three ways, and each became a design rule: a
  one-step decay rollback (−1.0 to −1.4%); caller-ID concentration (−3 to −17% DID health); hourly monitors at
  loose alphas (a false decay demoted a real winner, −1.6%).
- **Where we are weak:**
  - nw_renewal's engagement gap captured is low (2–17%), because its best levers are per-cohort (one voice for
    seniors) and cannot be seen at this volume (§13);
  - KYC engagement (the guardrail there) ends about 1.3 points lower: 0.584 vs 0.597 on public_01, which is
    −2.2% relative. Call-time weights reach more people in the age groups that engage less. That is inside a
    2-point absolute guardrail (assumption 1) but would breach a 2% relative one. We don't yet monitor
    workflow-level guardrail drift caused by composition (§13);
  - decay rollback completes 2–3 days after onset.
- **What I'd change next:** per-workflow index arrays (speed); a per-cohort decay watch for promoted winners at
  load volume; a calibration report for impact scores.

**Load (public_03):** conclusion rate +19.2% across 12 workflows; 54 changes applied (neutral: 16, beneficial: 38). Full table in `reports/public_03_load.md`, loop health in `reports/public_03_load_status.md`.

**Generalisation check:** we ran a copy of public_02 with a different seed and every incident shifted a day
(nothing re-tuned): **+25.6% conclusions, 0 harmful of 38 changes, prompt fixed in 105 min.**

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson-sampling bandit | Faster exploitation | Every reallocation is a governance request (2–3 a day). A bandit also makes "the LLM/statistics decided" harder to explain, and winner selection must be a deterministic test. Fixed allocation and staged looks instead |
| Bigger exploration share (50%) | More power | The worst-case harm crosses the 1% line at the first change. 30% cap plus an early drop instead |
| One-step decay rollback | Simpler, faster | Harmful when it lands while the champion is still marginally ahead. 50/50 demotion first instead |
| Flag a DID at a ratio of 0.8 | Faster detection | Wear alone produces 0.8, which caused cascades. 0.7 and z < −4, plus spreading |
| Per-cohort experiments | The truth is heterogeneous (seniors and the young respond differently) | About 15 connects per arm per day per cohort at dev volume. Coarse cohorts for timing and analysis; per-cohort arms are the first thing to add at load volume |
| Kafka/Redis for ingestion | "Real" streaming | Chunks are already durable and ordered on the server; a cursor plus a transaction is enough. SRE asked for no new state |
| Postgres locally | Matches production | A container to operate for a take-home. Plain SQL ports later |
| Calling the LLM on every incident | It might find better prompts | 3 credits for random candidates. The previous prompt has engagement evidence and is free. LLM only as a fallback |
| Auto-apply welcome/voice on Kestrel | The policy allows it | Customer-facing bets should be seen by a human (Compliance, Ops). Safety moves only |

## 13. Known weaknesses

1. **Decay detection is slow at low volume.** The 15% holdout gets about 140 connects a day, so champion vs
   holdout has power only for large drops; the drift signal helps but needs a day of data. Rollback finished
   2–3 days after onset.
2. **No per-cohort allocation.** Opposite effects across cohorts (the public truth has voices that help one age
   group and hurt another) average out at workflow level, and we leave that gain on the table.
3. **Exploration is the riskiest single change.** It is bounded by the 30% cap, not eliminated. A challenger set
   averaging −4% or worse would make the launch "harmful" by the grader's definition.
4. **The guardrail cannot be proven** at these volumes, only "not significantly breached". Call-time weights
   shift who connects. On KYC, engagement ends about 1.3 points lower (−2.2 to −2.5% relative): fine under an
   absolute reading of `max_drop`, a breach under a relative one. There is no workflow-level guardrail monitor
   after schedule changes yet. That is the next thing to add: compare the post-change week with the pre-change
   week, and soften the weights on a significant breach.
5. **Schedule gain is an estimate** from history. The dry run assumes the eligible-pool mix matches cohort
   shares; the real gain is smaller as good hours deplete. Weights are set once and not re-learned.
6. **A flagged caller ID recovers slowly**: its 3% probe needs about 2 days for enough dials to unflag it.
7. **Impact scores are not calibrated** against realised impact.
8. **Memory** is about 1.3 GB at load (every call in memory); see §8.
9. **Our approver-timing model** comes from the docs. If approvers counted latency only in working minutes, some
   late-afternoon requests would land the next day (still inside the TTL in every policy we saw).

## 14. If this went to production at Formi next month

1. **Shadow mode, days 1–3:** read-only against production events. Plans are written to the audit store but
   nothing is submitted. Compare dry runs with what actually happened.
2. **Postgres migration** and **tenant sharding with an advisory lock**, plus the rolling-window aggregation
   (§8 items 1–3). Load-test at 2× the envelope.
3. **Turn on operational changes only** (call time, caller ID) for one tenant whose policy allows auto-apply,
   with the weekly summary Compliance asked for.
4. **Approvals for one workflow**, with Kestrel's supervisor in the loop. Measure the approve/reject rate and
   time-to-decision, and tune the batching.
5. **The incident path everywhere** (validate shipped prompts). It is the cheapest and highest-value loop.
6. **Wire alerts** into their pager, and run a game day: kill pods during the 21:30 deploy and confirm nothing
   double-applies.
7. Only then, **variant experiments** at production volume, with per-cohort arms where the volume supports them.
