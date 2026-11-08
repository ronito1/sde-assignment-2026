# Supervisor view — as of D10 00:00

## ks_cancel (kestrel_finance) — primary engagement_rate, guardrail {'pickup_rate_max_drop': 0.02}

**Live config v15** (controller_auto): welcome {'W1': 0.15, 'W2': 0.85} · voice {'V1': 0.7, 'V2': 0.15, 'V3': 0.15} · caller ID {'D1': 0.4436, 'D2': 0.03, 'D3': 0.5264} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **PROMOTED** control W1, champion **W2** — W2: +9.4% (n=1019, p=0.00374), W3: +1.8% (n=1109, p=0.29843)
- voice: **ENDED** control V1 — V2: +2.0% (n=808, p=0.295), V3: -1.8% (n=801, p=0.68677)
- voice: **RUNNING** control V1 — V2: +12.5% (n=304, p=0.01953), V3: -0.7% (n=302, p=0.54682)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- none

**Incidents:**

- D3 12:00 hallucination on P2 (0.0633 vs 0.0153) — RESOLVED, resolved D3 15:30

**Caller IDs flagged unhealthy:** ['D2']

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

**Live config v18** (controller_auto): welcome {'W1': 0.7, 'W2': 0.3} · voice {'V1': 0.15, 'V2': 0.7, 'V3': 0.15} · caller ID {'D1': 0.0972, 'D2': 0.7, 'D3': 0.106, 'D4': 0.0969} · prompt P1 · 4 cohorts
**Cohorts:** `age_band_31_45` {"age_band": ["31-45"]} (controller), `age_band_46_60` {"age_band": ["46-60"]} (controller), `age_band_18_30` {"age_band": ["18-30"]} (controller), `age_band_60` {"age_band": ["60+"]} (controller)

**Being tested / winning:**

- welcome_message: **ENDED** control W1, champion **W3** — W2: -5.6% (n=292, p=0.83117), W3: +23.1% (n=298, p=3e-05)
- voice: **ENDED** control V1 — V2: +3.4% (n=540, p=0.2041), V3: -5.5% (n=560, p=0.91136)
- welcome_message: **RUNNING** control W1 — W2: +2.4% (n=827, p=0.25479)
- voice: **RUNNING** control V2 — V1: -4.7% (n=403, p=0.83756), V3: -2.8% (n=426, p=0.72913)

**Waiting for you:**

- nothing

**Changes in the last 24h:**

- D9 10:00 v17 by controller_auto: ["caller_id"] spread calls across healthy caller IDs
- D9 19:00 v18 by controller_auto: ["caller_id"] spread calls across healthy caller IDs

