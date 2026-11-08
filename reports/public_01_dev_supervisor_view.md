# Supervisor view — as of D8 00:00

## ks_cancel (kestrel_finance) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v9** (controller_approved): welcome {'W1': 0.7, 'W2': 0.3} · voice {'V1': 0.15, 'V4': 0.85} · caller ID {'D1': 0.2943, 'D2': 0.3385, 'D3': 0.3672} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **ENDED** control W1 — W2: +3.2% (n=1086, p=0.15416)
- voice: **PROMOTED** control V1, champion **V4** — V2: -0.3% (n=239, p=0.51453), V3: +1.1% (n=244, p=0.43967), V4: +25.1% (n=253, p=0.00014)
- welcome_message: **RUNNING** control W1 — W2: +7.0% (n=297, p=0.14509)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- D7 11:45 v9 by controller_approved: ["welcome_message"] Test W2 against W1 for welcome message on 30% of calls

## nw_kyc (northwind_life) — primary pickup_rate, guardrail {'engagement_rate_max_drop': 0.02}

**Live config v3** (controller_approved): welcome {'W1': 1.0} · voice {'V1': 1.0} · caller ID {'D1': 0.03, 'D2': 0.485, 'D3': 0.485} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- nothing (workflow's primary KPI is not moved by welcome/voice, or no test started yet)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

## nw_renewal (northwind_life) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v10** (controller_approved): welcome {'W1': 0.7, 'W2': 0.3} · voice {'V1': 0.7, 'V2': 0.1, 'V3': 0.1, 'V4': 0.1} · caller ID {'D1': 0.3112, 'D2': 0.3341, 'D3': 0.3247, 'D4': 0.03} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **ENDED** control W1, champion **W3** — W2: +7.9% (n=255, p=0.10877), W3: +19.1% (n=253, p=0.00148)
- voice: **RUNNING** control V1 — V2: +0.6% (n=772, p=0.43259), V3: -4.5% (n=731, p=0.90285), V4: -4.0% (n=757, p=0.87946)
- welcome_message: **RUNNING** control W1 — W2: -2.2% (n=176, p=0.61007)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- D7 13:30 v10 by controller_approved: ["welcome_message"] Test W2 against W1 for welcome message on 30% of calls

