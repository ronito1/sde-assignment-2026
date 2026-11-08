"""Every tunable in one place. Each value says why it is what it is.

Nothing here names a workflow from a public scenario. Per-tenant overrides are stakeholder agreements
(see SUBMISSION.md §3) keyed by tenant id; an unknown tenant simply gets the policy defaults.
"""

# ---------------------------------------------------------------- loop pacing
TICK = 15                    # simulator tick; every advance is a multiple of this
STEP_BUSY = 15               # step while an approval is in flight or an incident is open
STEP_IDLE = 60               # step otherwise during the working day
PLAN_EVERY_MIN = 60          # planners (schedule/caller-id/experiments) run at most hourly

# ---------------------------------------------------------------- statistics
ALPHA_WIN = 0.05             # family-wise one-sided alpha for declaring a winner, per experiment
MAX_LOOKS = 6                # alpha is split evenly over this many interim looks (conservative spending)
ALPHA_DROP = 0.01            # one-sided alpha for dropping a clearly-worse arm, checked every 6h (repeated looks!)
MIN_CONN_PER_ARM = 250       # no decision on an arm before this many connected calls
MIN_REL_LIFT = 0.02          # a "winner" must also beat control by >= 2% relative (point estimate)
GUARDRAIL_Z = -2.58          # a guardrail breach needs strong evidence (we cannot prove non-inferiority
                             # at these volumes; see SUBMISSION §5)

# ---------------------------------------------------------------- experiments
EXPLORE_SHARE = 0.30         # total traffic given to challengers per dimension at launch. Caps the damage a bad
                             # challenger set can do before we can see it (30% x -3% = -0.9% expected engagement)
HOLDOUT_SHARE = 0.15         # traffic kept on the previous control after a promotion, to detect decay
DECAY_WINDOW_MIN = 24 * 60   # champion vs holdout is compared over this trailing window
DECAY_MIN_CONN = 120         # minimum holdout connects in that window before decay can be declared
DECAY_ALPHA = 0.01           # monitors re-test the same population every 6h for days: alpha must be small
DECAY_DRIFT_ALPHA = 0.005    # champion's own drift vs its first day after promotion
MONITOR_EVERY_MIN = 6 * 60   # drop / decay / guardrail decisions at most every 6h (stats still refresh hourly)
EXPERIMENT_COOLDOWN_MIN = 12 * 60   # after a rollback, wait before re-exploring that dimension

# ---------------------------------------------------------------- cohorts / schedule
MAX_COHORTS = 4              # coarse cohorts only: each must be able to fill an experiment arm in ~2 days
MIN_COHORT_SHARE = 0.10      # a feature value needs >=10% of dials to become its own cohort
SCHEDULE_PRIOR_DIALS = 80    # empirical-Bayes shrinkage of per-(cohort,hour) pickup toward the cohort mean
SCHEDULE_GAMMA = 2.0         # sharpening of the pickup ratio into a weight
SCHEDULE_CLIP = (0.3, 3.0)   # never 0 (unused capacity), never extreme (cadence/gap starvation)
SCHEDULE_MIN_GAIN = 0.01     # only propose a schedule if the estimated pickup gain is >= 1% relative

# ---------------------------------------------------------------- caller id
DID_WINDOW_MIN = 6 * 60      # detection window
DID_MIN_DIALS = 250          # per DID inside the window before it can be judged
DID_BAD_RATIO = 0.70         # pickup ratio vs same-hour pool below which a DID is unhealthy... (numbers also
                             # wear gradually with load; that is handled by spreading, not by flagging)
DID_BAD_Z = -4.0             # ...and only with this much evidence (population, not anecdote)
DID_MAX_SHARE = 0.70         # never concentrate: a number carrying everything wears out
DID_PROBE_SHARE = 0.03       # share given to an untested DID, and kept on a flagged one to see recovery
DID_REBALANCE_EVERY_MIN = 6 * 60
DID_MIN_EXP_GAIN = 0.01      # only rebalance for >= 1% estimated pickup gain
DID_PLAN_WINDOW_MIN = 24 * 60

# ---------------------------------------------------------------- incidents / credits
HALLUC_MIN_CONN = 300        # connects since the suspect change (or in the rolling window) before judging
HALLUC_WINDOW_CONN = 800     # rolling window for drift detection without a config change
HALLUC_P = 1e-3              # binomial tail p-value threshold
HALLUC_MIN_RATIO = 1.8       # and the flag rate must be at least this multiple of the history baseline
VALIDATION_N = 300           # 3 credits; enough to separate ~0.99 from <0.97 most of the time
CREDIT_RESERVE_SHARE = 0.40  # never spend the last 40% of credits on anything but an open incident
LLM_COST = 3

# ---------------------------------------------------------------- governance
INCIDENT_SLOTS = 1           # requests per 24h kept back from routine work for incident fixes
APPROVAL_SAFETY_MIN = 15     # submit only if worst-case decision lands this long before approver hours end
TENANT_OVERRIDES = {
    # Kestrel ops supervisor (stakeholders/04): at most 2 requests/day, never before 11:00.
    "kestrel_finance": {"max_requests_per_24h": 2, "earliest_submit_hour": 11},
}
CUSTOMER_FACING = {"welcome_message", "voice", "prompt_version"}

# ---------------------------------------------------------------- http
HTTP_TIMEOUT = 900.0
HTTP_RETRIES = 8
