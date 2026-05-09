# Audit D — Influencer and Macro Sub-Index Post-Phase-1 Re-Run

**Date:** 2026-05-10
**Original audit:** 2026-05-07
**Auditor:** Claude Code
**Sprints deployed since original:** 1, 2, 3, 4, 5a, 6

---

## Summary

Two of the twelve original findings have been resolved by Sprint 4 (z-score normalization and per-source half-lives). The remaining ten findings are unchanged. No regressions were introduced by Sprints 1-6.

Sprint 4 delivered:
- **D-C1 (Critical):** Z-score normalization now applies to `insider_net_shares`, `analyst_buy_pct`, and `vix` via the `RollingZScorer` class. Parametric fallback is correctly retained for insufficient history. `analyst_target_price` and `sector_etf_return_20d` remain parametric-only by design decision.
- **D-S5 (Significant):** Per-source half-lives replace the blanket 48-hour decay. Influencer layer defaults to 72h (3 days) for analyst signals, with a source-specific override of 168h (7 days) for `sec_edgar` filings. Macro layer uses 336h (14 days). All three values match the paper specification.

---

## Gap Resolution Table

| Original ID | Gap Description | Original Severity | Sprint Fix | Current Status | Evidence |
|---|---|---|---|---|---|
| D-C1 | No z-score normalization for influencer/macro signals | Critical | Sprint 4 | **RESOLVED** | `_ZSCORE_CONFIG` in `normalize.py` lines 197-199 includes `insider_net_shares` (window=90), `analyst_buy_pct` (window=90), `vix` (window=90, negate=True). `score_influencer_signals()` (line 466-536) and `score_macro_signals()` (line 539-604) both attempt z-score first, falling back to parametric. Telemetry via `_record_method()` tracks usage. |
| D-S1 | Macro daily-only cadence; paper specifies hourly during trading hours | Significant | None | **OPEN** | `scheduler.py` line 606-614: `macro_job` still uses `CronTrigger(hour=2, minute=0)` — once daily at 02:00 UTC. No intraday VIX or sector ETF refresh exists. |
| D-S2 | SEC Form 4 ingestion cadence is every 6 hours; paper implies near-real-time | Significant | None | **OPEN** | `scheduler.py` line 596-604: `influencer_job` still uses `IntervalTrigger(hours=6)`. |
| D-S3 | No earnings estimate / EPS surprise signals | Significant | None | **OPEN** | No new signal types added for earnings. `_INFLUENCER_SIGNAL_TYPES` in `orchestrator.py` line 110-112 remains `[insider_net_shares, analyst_buy_pct, analyst_target_price]`. |
| D-S4 | No interest rates / liquidity indicators (Fed Funds, 10Y yield, TED spread) | Significant | None | **OPEN** | `_MACRO_SIGNAL_TYPES` in `orchestrator.py` line 113 remains `["vix", "sector_etf_return_20d"]`. No Treasury yield or liquidity signals exist. |
| D-S5 | 48-hour half-life inappropriate for filings (7d) and macro (14d) | Significant | Sprint 4 | **RESOLVED** | `_LAYER_HALF_LIFE_H` in `normalize.py` line 60-65: `influencer=72.0` (3 days, analyst default), `macro=336.0` (14 days). `_HALF_LIFE_OVERRIDE` line 68-69: `("influencer", "sec_edgar")=168.0` (7 days for filings). `_get_half_life()` line 73-78 correctly looks up the override first, then the layer default. |
| D-S6 | Analyst signals are levels (latest snapshot) not changes | Significant | None | **OPEN** | `_analyst_recommendations()` in `influencer.py` line 250-289 returns `(strongBuy + buy) / total` from the most recent Finnhub recommendation period. No delta computation against prior values. `analyst_target_price` similarly returns `targetMean` as-is (line 292-317). |
| D-S7 | Analyst signals (recommendation, price-target) blocked on Finnhub free tier | Significant | None | **OPEN** | `influencer.py` lines 264-265 and 301-302 still show `_log.debug("... unavailable ...")` for 403 responses. No alternative source configured. |
| D-S8 | `wauthor` (insider authority weight) missing for insider transactions | Significant | None | **OPEN** | `_fetch_form4_transactions()` in `influencer.py` does not extract the reporting person's relationship or title. `_insider_finnhub()` similarly does not weight by insider role. All insider transactions receive equal source weight (1.0 for sec_edgar, 0.8 for finnhub). |
| D-K1 | Sector ETF returns are global, not mapped to per-ticker GICS sector | Cosmetic | None | **ACCEPTED** | `_score_macro()` in `orchestrator.py` line 265-284 fetches all sector ETF returns and feeds them to `score_macro_signals()` as a single pool. No per-ticker sector mapping exists. Original audit accepted this as a cosmetic tradeoff. |
| D-K2 | `sector_etf_close` ingested but never scored (only `sector_etf_return_20d` scored) | Cosmetic | None | **OPEN** | `score_macro_signals()` in `normalize.py` line 561 explicitly filters to `("vix", "sector_etf_return_20d")`, excluding `sector_etf_close`. The close values are ingested (`macro.py` line 238) and used only to compute the 20-day return. |
| D-K3 | `analyst_buy_pct` effective range is [25, 75] due to parametric scorer | Cosmetic | None | **OPEN** (mitigated) | Parametric scorer `_score_analyst_buy_pct()` at `normalize.py` line 286-287 still maps to [25, 75]. However, with Sprint 4's z-score path active (`_ZSCORE_CONFIG` includes `analyst_buy_pct` with window=90), the z-score scorer maps to the full [0, 100] range when sufficient history exists (>= 45 observations at 50% fill threshold). The parametric [25, 75] limitation only applies during the cold-start period. |

---

## Detailed Findings (changed status only)

### D-C1: Z-Score Normalization — RESOLVED

**Sprint 4 implementation verified.** The `RollingZScorer` class (lines 98-181 of `pipeline/features/normalize.py`) provides adaptive z-score normalization with the following configuration for influencer and macro signals:

```python
# From _ZSCORE_CONFIG (normalize.py lines 186-200):
"insider_net_shares":     RollingZScorer(window=90),
"analyst_buy_pct":        RollingZScorer(window=90),
"vix":                    RollingZScorer(window=90, negate=True),
```

Key design decisions verified:
- **`analyst_target_price`** is intentionally excluded from z-score config. The `score_influencer_signals()` function (line 515-516) handles it separately via the parametric `_score_analyst_target()` which computes upside relative to current price. Z-score would be inappropriate here because the raw target price depends on the stock's absolute price level.
- **`sector_etf_return_20d`** is intentionally excluded from z-score config. The docstring in `score_macro_signals()` (line 550-551) notes "deferred -- insufficient per-ETF history." This is reasonable: with 11 ETFs each at daily cadence, accumulating 45+ observations per ETF would take ~45 trading days (~9 weeks).
- **`vix`** uses `negate=True`, which correctly maps higher VIX (more fear) to lower scores (more bearish).
- The z-score formula correctly clamps to +/-3 sigma and maps to [0, 100] via `50 + 50 * (z / 3)` (line 172).
- Parametric fallback is always available: when `score_from_history()` returns `None` (insufficient data or below sigma floor), the existing parametric scorer is used (lines 519-529 for influencer, lines 586-592 for macro).
- Telemetry via `_record_method()` and `log_scoring_telemetry()` tracks z-score vs parametric usage per signal type.

**Fill gate** (Sprint 4): The `fill_threshold=0.5` default means the effective minimum observations for window=90 signals is `max(30, ceil(90 * 0.5)) = 45`. This prevents the z-score from activating on a sparse history that could produce misleading statistics.

### D-S5: Per-Source Half-Lives — RESOLVED

**Sprint 4 implementation verified.** The blanket 48-hour half-life has been replaced with a per-layer + per-source override system:

```python
# From normalize.py lines 60-69:
_LAYER_HALF_LIFE_H = {
    "market":     1.0,       # 60 min
    "narrative":  12.0,      # 12 hours
    "influencer": 72.0,      # 3 days (analyst default)
    "macro":      336.0,     # 14 days
}

_HALF_LIFE_OVERRIDE = {
    ("influencer", "sec_edgar"): 168.0,   # SEC filings: 7 days
}
```

Verification of the lookup path in `_get_half_life()` (line 73-78):
- `_get_half_life("influencer", "sec_edgar")` returns 168.0 (7 days) -- override hit.
- `_get_half_life("influencer", "finnhub")` returns 72.0 (3 days) -- layer default (analyst signals from Finnhub).
- `_get_half_life("macro", "alpha_vantage")` returns 336.0 (14 days) -- layer default.
- `_get_half_life("macro", "yfinance")` returns 336.0 (14 days) -- layer default.
- `_get_half_life("macro", "computed")` returns 336.0 (14 days) -- layer default (sector_etf_return_20d).

The `_build()` function (line 327-347) uses `_get_half_life(layer, source)` to compute time decay, confirming the half-lives are applied at the individual signal level (not just at the layer level).

All three paper-specified values are correctly implemented:
- Analyst signals: 3 days (72 hours)
- SEC filings: 7 days (168 hours)
- Macro signals: 14 days (336 hours)

---

## Findings Unchanged

### D-S1: Macro Daily-Only Cadence — OPEN

`macro_job` remains scheduled at `CronTrigger(hour=2, minute=0)` in `scheduler.py` line 606-614. The paper specifies "hourly during trading hours, daily otherwise" for VIX and sector ETFs. No intraday macro refresh has been added. VIX data can be up to 22 hours stale before the next ingestion cycle.

### D-S2: SEC Form 4 Ingestion Cadence — OPEN

`influencer_job` remains on `IntervalTrigger(hours=6)` in `scheduler.py` line 596-604. The paper implies near-real-time insider filing detection. The 6-hour cadence means insider transactions can be delayed up to 6 hours from EDGAR publication to ingestion.

### D-S3: No Earnings Estimate / EPS Surprise Signals — OPEN

No new signal types for earnings estimates, EPS surprises, or revenue surprises have been added. The influencer layer is limited to insider transactions and analyst consensus (buy/sell ratings + price targets).

### D-S4: No Interest Rates / Liquidity Indicators — OPEN

The macro layer remains limited to VIX and sector ETF returns. No Treasury yields (10Y, 2Y, Fed Funds), credit spreads (TED spread, corporate bond spreads), or other macroeconomic indicators have been added.

### D-S6: Analyst Signals Are Levels, Not Changes — OPEN

`_analyst_recommendations()` (`influencer.py` line 250-289) returns the current buy percentage as a level (0-1 scale). No delta or change-over-time computation is performed. The z-score normalization (D-C1 fix) partially mitigates this by contextualizing the current level against its own historical distribution, but the fundamental issue remains: a stable 60% buy rating looks the same whether it just upgraded from 40% or downgraded from 80%.

### D-S7: Analyst Signals Blocked on Finnhub Free Tier — OPEN

The `/stock/recommendation` and `/stock/price-target` endpoints return 403 on Finnhub's free tier. No alternative data source has been configured. This means `analyst_buy_pct` and `analyst_target_price` are effectively absent for deployments using Finnhub free-tier API keys.

### D-S8: `wauthor` Missing for Insider Transactions — OPEN

`_fetch_form4_transactions()` in `influencer.py` (lines 103-204) parses Form 4 XML for transaction date, shares, and acquired/disposed code, but does not extract:
- `reportingPerson/reportingPersonTitle/value` (e.g., CEO, CFO, Director)
- `reportingPerson/officerTitle` (specific officer role)
- Relationship type (officer, director, 10% owner)

All insider transactions receive equal weight regardless of the insider's position. The paper specifies an authority weight (`wauthor`) that should upweight C-suite transactions relative to director or 10% owner transactions.

### D-K1: Sector ETF Returns Global, Not Per-Ticker — ACCEPTED

All 11 sector ETF 20-day returns are pooled together and fed to every ticker's macro sub-index identically. No GICS sector mapping exists to weight the ticker's own sector ETF more heavily. This was accepted as a cosmetic tradeoff in the original audit.

### D-K2: `sector_etf_close` Ingested But Not Scored — OPEN

`macro.py` ingests `sector_etf_close` signals (line 238) for each sector ETF, but `score_macro_signals()` in `normalize.py` (line 561) explicitly filters to only `vix` and `sector_etf_return_20d`. The close values serve as intermediate data used to compute the 20-day return and are not directly scored. This is arguably correct design (close prices alone have no sentiment information without a return calculation), but the original audit flagged it as a potential signal waste.

### D-K3: `analyst_buy_pct` Parametric Range [25, 75] — OPEN (mitigated)

The parametric fallback `_score_analyst_buy_pct()` (line 286-287) still maps the 0-1 range to [25, 75]:
```python
def _score_analyst_buy_pct(pct: float) -> float:
    return max(0.0, min(100.0, 25.0 + pct * 50.0))
```

However, Sprint 4's z-score normalization mitigates this for tickers with sufficient history. When the z-score path activates (>= 45 observations), `analyst_buy_pct` is scored on the full [0, 100] range via the z-score formula. The parametric [25, 75] limitation only applies during the cold-start period (first ~45 data points, which at 6-hour ingestion cadence is approximately 11 days).

---

## Regressions

No regressions detected. All Sprint 1-6 changes are additive or refinements:

1. **Sprint 3 decoupling intact:** `influencer_job` and `macro_job` remain data-only. Neither calls any scoring function. Scoring is performed exclusively by `scoring_tick_job` via `_score_and_write()` -> `_score_influencer()` / `_score_macro()`. Verified in `scheduler.py` lines 437-493.

2. **Sub-index computation unchanged:** `compute_sub_index()` in `subindices.py` (lines 46-81) still uses the weighted-average + shrinkage formula. The influencer and macro layers use this generic path (not the structured `compute_market_sub_index()`). No changes to the aggregation logic.

3. **Orchestrator flow preserved:** `_score_influencer()` (lines 234-262) and `_score_macro()` (lines 265-284) follow the same read-DB -> normalize -> sub-index -> fallback pattern. The only Sprint 4 change is that `score_influencer_signals()` and `score_macro_signals()` are now `async` (they call `get_signal_history()` for z-score lookups), and the orchestrator correctly `await`s them.

4. **Staleness thresholds unchanged:** `staleness.py` thresholds remain at analyst=3 days, insider=30 days, macro=24 hours. No Sprint 1-6 changes affected these values.

5. **Signal ingestion unchanged:** `influencer.py` and `macro.py` source modules have not been modified since the original audit. The same signals are produced with the same sources and cadences.

---

## Conclusion

Sprint 4 successfully resolved 2 of the 12 original findings:
- **D-C1 (Critical):** Z-score normalization for influencer and macro signals is fully implemented with appropriate fallback, fill gating, and telemetry.
- **D-S5 (Significant):** Per-source half-lives correctly differentiate analyst (3d), filings (7d), and macro (14d) decay rates.

The remaining 10 findings (7 Significant, 1 Accepted, 2 Cosmetic) are unchanged and remain in scope for future sprints. The most impactful open gaps are:
- **D-S1** (macro daily cadence) and **D-S7** (analyst signals blocked on free tier) together mean that the influencer and macro sub-indices may operate on significantly stale or absent data for many deployments.
- **D-S3** (no earnings) and **D-S4** (no interest rates) represent missing signal categories that limit the expressiveness of the influencer and macro layers respectively.

No regressions were introduced by Sprints 1-6.
