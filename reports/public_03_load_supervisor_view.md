# Supervisor view — as of D5 00:00

## arav_cancel_2 (aravalli_general) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v8** (controller_approved): welcome {'W1': 0.15, 'W2': 0.85} · voice {'V1': 0.15, 'V4': 0.85} · caller ID {'D1': 0.3252, 'D2': 0.3074, 'D3': 0.3674} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W2** — W2: +4.9% (n=4009, p=0.00141)
- voice: **PROMOTED** control V1, champion **V4** — V2: +0.4% (n=240, p=0.47428), V3: +2.5% (n=241, p=0.35768), V4: +16.5% (n=254, p=0.0061)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

## arav_kyc_1 (aravalli_general) — primary pickup_rate, guardrail {'engagement_rate_max_drop': 0.02}

**Live config v3** (controller_approved): welcome {'W1': 1.0} · voice {'V1': 1.0} · caller ID {'D1': 0.03, 'D2': 0.4824, 'D3': 0.4876} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- nothing (workflow's primary KPI is not moved by welcome/voice, or no test started yet)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Caller IDs flagged unhealthy:** ['D1']

## arav_renewal_0 (aravalli_general) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v6** (controller_approved): welcome {'W1': 0.15, 'W3': 0.85} · voice {'V1': 0.7999999999999999, 'V3': 0.1, 'V4': 0.1} · caller ID {'D1': 0.30636936306369367, 'D2': 0.32086791320867913, 'D3': 0.3427657234276572, 'D4': 0.029997000299970003} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W3** — W2: -5.0% (n=423, p=0.85499), W3: +14.0% (n=431, p=0.00123)
- voice: **RUNNING** control V1 — V3: -1.4% (n=7369, p=0.91182), V4: -0.2% (n=7090, p=0.57422)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Caller IDs flagged unhealthy:** ['D4']

## kest_cancel_2 (kestrel_finance) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v9** (controller_approved): welcome {'W1': 0.15, 'W2': 0.85} · voice {'V1': 0.15, 'V4': 0.85} · caller ID {'D1': 0.3150684931506849, 'D2': 0.31796820317968205, 'D3': 0.36696330366963303} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W2** — W2: +3.3% (n=6298, p=0.00592)
- voice: **PROMOTED** control V1, champion **V4** — V3: -1.1% (n=1247, p=0.64069), V4: +8.2% (n=1254, p=0.00349)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

## kest_kyc_1 (kestrel_finance) — primary pickup_rate, guardrail {'engagement_rate_max_drop': 0.02}

**Live config v3** (controller_approved): welcome {'W1': 1.0} · voice {'V1': 1.0} · caller ID {'D1': 0.03, 'D2': 0.4801, 'D3': 0.4899} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- nothing (workflow's primary KPI is not moved by welcome/voice, or no test started yet)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- D4 15:45 v3 by controller_approved: ["caller_id"] Move calls off unhealthy caller ID(s) ['D1']

**Caller IDs flagged unhealthy:** ['D1']

## kest_renewal_0 (kestrel_finance) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v5** (controller_approved): welcome {'W1': 0.15, 'W3': 0.85} · voice {'V1': 0.7, 'V2': 0.1, 'V3': 0.1, 'V4': 0.1} · caller ID {'D1': 0.3066, 'D2': 0.3168, 'D3': 0.3466, 'D4': 0.03} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W3** — W2: +7.0% (n=1870, p=0.00139), W3: +14.1% (n=1920, p=0.0)
- voice: **RUNNING** control V1 — V2: +0.0% (n=7253, p=0.49153), V3: -0.5% (n=7225, p=0.68528), V4: -0.6% (n=7077, p=0.72667)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Caller IDs flagged unhealthy:** ['D4']

## nort_cancel_2 (northwind_life) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v8** (controller_approved): welcome {'W1': 0.15, 'W2': 0.85} · voice {'V1': 0.15, 'V4': 0.85} · caller ID {'D1': 0.3141, 'D2': 0.315, 'D3': 0.3709} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W2** — W2: +3.5% (n=8256, p=0.00131)
- voice: **PROMOTED** control V1, champion **V4** — V2: -0.9% (n=2606, p=0.66408), V3: -0.9% (n=2655, p=0.66549), V4: +7.0% (n=2602, p=0.00045)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- D4 11:30 v8 by controller_approved: ["welcome_message"] Make W2 the main welcome message (+3.5% engagement vs W1), keep 15% on W1 to watch for decay

## nort_kyc_1 (northwind_life) — primary pickup_rate, guardrail {'engagement_rate_max_drop': 0.02}

**Live config v3** (controller_approved): welcome {'W1': 1.0} · voice {'V1': 1.0} · caller ID {'D1': 0.03, 'D2': 0.485, 'D3': 0.485} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- nothing (workflow's primary KPI is not moved by welcome/voice, or no test started yet)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Caller IDs flagged unhealthy:** ['D1']

## nort_renewal_0 (northwind_life) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v5** (controller_approved): welcome {'W1': 0.15, 'W3': 0.85} · voice {'V1': 0.7, 'V2': 0.1, 'V3': 0.1, 'V4': 0.1} · caller ID {'D1': 0.3117, 'D2': 0.3188, 'D3': 0.3395, 'D4': 0.03} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W3** — W2: +9.1% (n=1415, p=0.0004), W3: +16.8% (n=1507, p=0.0)
- voice: **RUNNING** control V1 — V2: -1.7% (n=7008, p=0.94486), V3: -1.1% (n=7007, p=0.83972), V4: +1.2% (n=7001, p=0.1384)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Caller IDs flagged unhealthy:** ['D4']

## tide_cancel_2 (tidewater_health) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v8** (controller_approved): welcome {'W1': 0.85, 'W2': 0.15} · voice {'V1': 0.15, 'V4': 0.85} · caller ID {'D1': 0.3176, 'D2': 0.3053, 'D3': 0.3771} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **RUNNING** control W1 — W2: +2.1% (n=10246, p=0.02116)
- voice: **PROMOTED** control V1, champion **V4** — V3: +1.1% (n=2612, p=0.30575), V4: +8.5% (n=2509, p=4e-05)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

## tide_kyc_1 (tidewater_health) — primary pickup_rate, guardrail {'engagement_rate_max_drop': 0.02}

**Live config v3** (controller_approved): welcome {'W1': 1.0} · voice {'V1': 1.0} · caller ID {'D1': 0.03, 'D2': 0.485, 'D3': 0.485} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- nothing (workflow's primary KPI is not moved by welcome/voice, or no test started yet)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Caller IDs flagged unhealthy:** ['D1']

## tide_renewal_0 (tidewater_health) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v5** (controller_approved): welcome {'W1': 0.15, 'W3': 0.85} · voice {'V1': 0.7, 'V2': 0.1, 'V3': 0.1, 'V4': 0.1} · caller ID {'D1': 0.3164, 'D2': 0.3198, 'D3': 0.3338, 'D4': 0.03} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W3** — W2: +4.9% (n=1900, p=0.01712), W3: +15.4% (n=1852, p=0.0)
- voice: **RUNNING** control V1 — V2: +2.0% (n=7323, p=0.02844), V3: +1.3% (n=7285, p=0.11452), V4: +0.6% (n=7264, p=0.2711)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Caller IDs flagged unhealthy:** ['D4']

