# Controller status

- sim time D10 00:00 · iterations 263 · wall 7s
- events ingested 137078 at 81,037 events/s of ingest time
- time split: ingest 2s · decide 3s · simulator advance 2s
- peak RSS 546 MB · credits remaining 194.0 · max events behind at a decision 0

| sim day | wall s | events | decide s |
|---|---|---|---|
| D0 | 1 | 13011 | 0.2 |
| D1 | 1 | 13708 | 0.2 |
| D2 | 1 | 13710 | 0.4 |
| D3 | 1 | 13707 | 0.3 |
| D4 | 1 | 14232 | 0.3 |
| D5 | 1 | 13707 | 0.5 |
| D6 | 1 | 13185 | 0.3 |
| D7 | 1 | 13707 | 0.4 |
| D8 | 1 | 13706 | 0.3 |
| D9 | 1 | 13705 | 0.2 |
| D10 | 0 | 700 | 0.0 |

**Decisions by kind:** proposal_held 35, applied 31, caller_id_live 19, experiment_look 18, proposal_submitted 15, experiment_started 7, schedule_live 3, did_flagged 3, cohorts_chosen 3, validation_run 2, supervisor_edit 2, experiment_promoted 2, experiment_arm_dropped 2, did_recovered 2, rollback_step 1, incident_resolved 1, incident_opened 1, incident_fix_applied 1, experiment_ended_by_supervisor 1, demote_step 1, decay_suspected 1, decay_detected 1, controller_started 1, controller_finished 1, apply_failed 1
**Credit spend:** validation_run ×2 = 6

**Alerts** (page = wake someone; warn = next business day):

- D3 12:00 [page] ks_cancel hallucination_incident: ks_cancel: newly shipped prompt P2 failed validation (0.9367) → look at: python -m controller why --workflow ks_cancel; a fix is being proposed

**Proposals:** approval/APPLIED 12, approval/REJECTED 2, approval/STALE 1, auto/APPLIED 19
