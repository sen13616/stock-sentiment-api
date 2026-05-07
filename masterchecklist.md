# SentimentMarkets / SentimentAPI — Master Checklist

**Last updated:** 2026-05-07
**Status legend:** ☐ not started, ◐ in progress, ☑ done, ⊘ deferred

---

## Phase 1 — Foundational methodology compliance

The minimum work to make the running implementation match the paper's core methodology so the validation chapter is meaningful.

### 1.2 Architecture fixes (Tier 1 from Audit D)

These are the load-bearing fixes. Each unlocks downstream work.

- ☐ **G-C2 — Implement global scoring tick**
    - ☐ Add `scoring_tick_job` to scheduler, every 30 min on the half-hour
    - ☐ Refactor `_score_and_write()` to default to `layers=None` (recompute all)
    - ☐ Remove `_score_all()` calls from individual ingestion jobs (market_job, narrative_job, influencer_job, macro_job become data-collection-only)
    - ☐ Remove or repurpose `_carry_forward_layer()` — no longer needed
    - ☐ Update tests (scoring tests, scheduler tests, fallback tests)
    - ☐ Verify post-deploy: `*_as_of` timestamps in `sentiment_history` are aligned within seconds
    - ☐ Verify post-deploy: row count drops to ~48/ticker/day (paper's expected volume)

- ☐ **G-C1 + G-S1 combined — Z-score normalization + per-source half-lives**
    - ☐ Implement `RollingZScorer` class: per-ticker per-signal 90-day rolling μ/σ
    - ☐ Replace fixed parametric scorers (`_score_return`, `_score_rsi`, `_score_volume_ratio`, `_score_order_flow_imbalance`, `_score_bid_ask_spread_bps`) with z-score path
    - ☐ Apply sigma floor (meaningful threshold, e.g., 1e-4 not 1e-12)
    - ☐ Apply ±3 winsorization symmetrically
    - ☐ Apply minimum 30-observation gate (return None if < 30, exclude from sub-index)
    - ☐ Apply per-source half-lives (market 60min, news 12h, analyst 3d, filings 7d, macro 14d) in `_time_weight()`
    - ☐ Update Market sub-index aggregator to use the normalized z-scores (resolves G-S2 — aggregator currently ignores weights)
    - ☐ Update `short_volume_ratio_otc` z-score window from 20 to 90 days, min observations 10 → 30
    - ☐ Backfill historical `sentiment_history` not required (forward-only is fine)
    - ☐ Re-run audits A and D against post-fix code to verify. Self-check and if there any any red flags stop and let me know.

- ☐ **G-C3 — EMA smoothing**
    - ☐ Schema migration: add `composite_score_smoothed FLOAT` to `sentiment_history`
    - ☐ Implement `_ema_smooth(raw, last_smoothed, dt, half_life=4h)` — formula α = 1 - 0.5^(dt/half_life)
    - ☐ Pull previous smoothed value from Redis state in scoring path
    - ☐ Compute and persist `composite_score_smoothed` alongside `composite_score`
    - ☐ Decide API contract: which field does API serve by default? (recommendation: serve smoothed, expose raw as additional field)
    - ☐ Backfill: walk-forward through historical `sentiment_history` rows applying EMA to populate `composite_score_smoothed`
    - ☐ Update API response assembler to include smoothed value
    - ☐ Update website / dashboard consumers if they read the field directly

### 1.3 Math fixes (Tier 2)

Smaller fixes that close specific methodology gaps without architecture changes.

- ☐ **G-S4 — Log returns instead of simple returns**
    - ☐ Change `_compute_returns()` to use `math.log(current/prev)` instead of `(current-prev)/prev`
    - ☐ Update tests
    - ☐ Note: this changes historical signal values for new computations only (existing rows in `raw_signals` stay)

- ☐ **G-S3 — Include `volume_ratio` in Market sub-index aggregator**
    - ☐ Decide approach: 6th component, or merge into existing component as volume confirmation
    - ☐ Update `_COMPONENT_TYPES` and component weights in `subindices.py`
    - ☐ Update tests
    - ☐ Document weight redistribution if existing weights changed

- ☐ **G-S6 — Add NaN/Inf validation to scoring path**
    - ☐ Add `if math.isnan(value) or math.isinf(value): continue` at top of scoring loops in `score_market_signals()` and equivalent
    - ☐ Optionally: add a database-side check (e.g., `WHERE value = value AND value != 'inf'::float`)
    - ☐ Add tests with NaN/Inf inputs

- ☐ **G-S13 — Lower AV news source weight from 1.0 to 0.7**
    - ☐ Update `_SOURCE_WEIGHTS["alpha_vantage"]` in `normalize.py`
    - ☐ This is a behavior-changing fix — capture before/after for ~10 baseline tickers

- ☐ **G-S14 — Add minimum relevance threshold for narrative articles**
    - ☐ Add `if relevance < 0.1: continue` in narrative scoring loop, or at the DB query level
    - ☐ Decide threshold value (paper doesn't specify; 0.1–0.2 is reasonable)

- ☐ **G-C6 — Wire semantic deduplication into pipeline**
    - ☐ Call `cluster_articles()` from `narrative_job` after article ingestion
    - ☐ Modify `get_articles_since()` to use `DISTINCT ON (COALESCE(event_cluster_id, id::text))` selecting highest-relevance article per cluster
    - ☐ Verify post-deploy: `event_cluster_id` populated for new rows, narrative sub-index reflects deduped events
    - ☐ Optional: backfill cluster IDs for historical articles (one-time job)

### 1.4 Behavior validation post-Phase-1

After Phase 1 fixes deploy, confirm the system still behaves sensibly:

- ☐ Capture 10-ticker baseline before Phase 1 deploys
- ☐ Capture 10-ticker post-Phase-1 baseline (one full scoring cycle later)
- ☐ Document expected score changes and verify they fall within bounds:
    - Composite scores will shift due to z-score normalization (probably most tickers change by 5-15 points)
    - Confidence scores may change due to weight-redistribution behavior
    - Smoothed score introduces a new field
- ☐ Update README with the methodology change point and date
- ☐ Tag a release in git as `v1.0-methodology-compliant`

---

## Phase 2 — Methodology depth

Adding the layers of sophistication the paper describes but Phase 1 didn't have time for. Most of these are "the paper says we should do this; we have a Phase 1 simplification in place."

### 2.1 Local NLP (the largest single Phase 2 work item)

- ☐ **G-C5 — Implement FinBERT scoring**
    - ☐ Add `pipeline/nlp/finbert.py` with model loading and inference
    - ☐ Decide deployment: in-pipeline (every article) or batched background job
    - ☐ Schema: add `finbert_pos`, `finbert_neg`, `finbert_neu` columns for class probabilities (enables `wconf` later)
    - ☐ Populate `finbert_score` field for new articles
    - ☐ Backfill: run on historical articles where `finbert_score IS NULL`
    - ☐ Decide model variant (FinBERT-tone, FinBERT-FLS, ProsusAI/FinBERT)
- ☐ **G-C4 — Use FinBERT scores for Finnhub articles**
    - ☐ Modify `get_articles_since()` to use `COALESCE(finbert_score, provider_sentiment)`
    - ☐ Validate: Finnhub articles now contribute to narrative sub-index
- ☐ **G-S11 — Implement `wconf` (model confidence)**
    - ☐ After class probabilities are stored, compute `wconf = max(P(k))` or `1 - H(P)`
    - ☐ Add to event-level weight calculation in `_build()`
- ☐ **G-S16 — Language detection**
    - ☐ Add `langdetect` or `fasttext` at ingestion time
    - ☐ Schema: add `language VARCHAR(10)` column
    - ☐ Filter to English only (or add language-specific FinBERT models if expanding)

### 2.2 ABSA (aspect-based sentiment)

- ☐ Schema: add `aspect_scores JSONB` to `raw_articles` (e.g. `{"revenue": 0.7, "margins": -0.3}`)
- ☐ Decide model approach: extract aspects via NER + score per aspect, or use ABSA-specific model
- ☐ Wire into composite or keep as separate analytical layer (decision)
- ☐ Update API response to expose aspect-level sentiment for Pro tier

### 2.3 Additional signal sources (G-S15, G-S17 through G-S22)

- ☐ **Earnings call transcripts** — source via SEC EDGAR 8-K filings or transcript provider
- ☐ **Reddit / forum sentiment** — via ApeWisdom (already in stack) or PushShift
- ☐ **Social media** — Twitter/X integration (subject to API access)
- ☐ **Hourly macro ingestion (G-S17)** — change `macro_job` from daily to hourly during trading hours
- ☐ **Hourly EDGAR ingestion (G-S18)** — separate `insider_job` from `analyst_job`, run insider hourly
- ☐ **Earnings estimate updates (G-S19)** — find data source (Finnhub paid? Alternative?)
- ☐ **Interest rates (G-S20)** — Treasury yields, Fed funds rate (FRED API, free)
- ☐ **Liquidity indicators (G-S20)** — TED spread, dollar liquidity (FRED)
- ☐ **Analyst rating changes vs levels (G-S21)** — store historical ratings and compute upgrade/downgrade events
- ☐ **`analyst_target_price` unblocking (G-S22)** — Finnhub paid tier, or alternative source

### 2.4 Author credibility (G-S12)

- ☐ Schema: add `author` and `author_credibility` columns to `raw_articles`
- ☐ For insiders: add `insider_role` (CEO, VP, board member) — already partly available via Form 4
- ☐ Implement `wauthor` weight in event-level weighting
- ☐ Decide credibility scheme (heuristic table, learned, or hybrid)

### 2.5 Macro stock-specificity (G-K10)

- ☐ Per-ticker sector mapping (already partially exists via `ticker_universe.sector`)
- ☐ Modify macro sub-index to use per-ticker sector ETF weight rather than uniform sector basket
- ☐ Document in paper as Phase 2 enhancement

### 2.6 Bootstrap confidence intervals (G-S9)

- ☐ Implement bootstrap confidence in `pipeline/confidence/`
- ☐ Schema: add `confidence_interval_low`, `confidence_interval_high` to `sentiment_history`
- ☐ Keep operational confidence for API serving; statistical confidence for validation chapter

---

## Phase 3 — Validation and publication

The work to validate the methodology empirically and write the paper.

### 3.1 Validation infrastructure (depends on Audit E)

- ☐ Forward returns construction (1d, 5d, 20d horizons matching signal periods)
- ☐ Train/test split methodology
- ☐ Backtesting harness — populate `backtest_results` table
- ☐ Information Coefficient computation per layer and composite
- ☐ Decile portfolio construction and performance tracking
- ☐ Lead-lag analysis (does sentiment lead returns?)
- ☐ Granger causality tests
- ☐ Event studies (around earnings, FDA approvals, etc.)
- ☐ Sub-index ablations (composite minus market, composite minus narrative, etc.)
- ☐ Regime-conditional performance (high-vol vs low-vol, bull vs bear)
- ☐ Baseline comparisons (price-only, momentum-only, naive sentiment)
- ☐ Robustness checks (different lookback windows, different weights)

### 3.2 Paper finalization

- ☐ Update Methodology section to reflect actual Phase 1 implementation (not aspirational)
- ☐ Document Phase 2 enhancements as future work
- ☐ Write Validation Methodology chapter using actual results
- ☐ Add limitations section honestly listing methodology divergences kept (high-low spread proxy decision, divergence cap, operational vs statistical confidence)
- ☐ Add company-specific macro to Future Extensions
- ☐ Internal review (you, then ideally one external reviewer)
- ☐ Submit to SSRN
- ☐ Identify target journal(s) for follow-up submission

---

## Phase 4 — Productization

Work specific to product and research outputs that aren't strictly the API methodology.

### 4.1 SentientMarkets website (separate repo)

- ☐ Hero trending tickers — pull from `/api/home → trending_tickers`
- ☐ MoodCard 5-day history bars — verify backend returns history array, wire or remove
- ☐ Volume bar `score={33}` static placeholder in `StockDetailPage.tsx` line 651 — compute or remove
- ☐ Stock page analyst/technical/institutional sections — replace mock data
- ☐ Domain migration to SentientMarkets on Namecheap + Cloudflare
- ☐ Use `composite_score_smoothed` (when Phase 1.2 G-C3 is done) as the displayed score

### 4.2 SentimentAPI productization

- ☐ Rate limiting per tier (free vs Pro) — verify implementation matches stated limits
- ☐ API documentation (probably docs.claude.com style — Stripe / Cloudflare style docs)
- ☐ `/sentiment/{ticker}/history` endpoint with configurable lookback for premium charts
- ☐ Pro tier billing integration (if not already done)
- ☐ Public-facing changelog when Phase 1 deploys

### 4.3 Tooling and observability

- ☐ Pipeline Health screen: add "writers that wrote 0 rows" alarm (the FINRA-pattern detector)
- ☐ Signal Catalog generation (the original purpose of the `feature/signal-catalog` branch — produce `docs/SIGNAL_CATALOG.md`)
- ☐ Methodology compliance dashboard (auto-runs the audits periodically and reports drift)
- ☐ Recurring audit reminder — quarterly audit of paper-vs-implementation

---

## Maintenance backlog

Items that need to happen but aren't time-critical, plus future enhancements.

### Cosmetic fixes from audits

- ☐ G-K1 — RSI dual-scoring cleanup (remove unused contrarian scorer or use it)
- ☐ G-K2 — Short volume min observation threshold 10 → 30 (if window is 90 days)
- ☐ G-K3 — Sigma floor threshold (1e-12 → meaningful value)
- ☐ G-K7 — Update CLAUDE.md composite weights (stale: 0.4/0.25/0.2/0.15; actual: 0.35/0.30/0.25/0.10)
- ☐ G-K9 — Persist `cap_applied` flag in `sentiment_history`
- ☐ G-K11 — `sector_etf_close` storage decision (drop or use for something)
- ☐ G-K12 — `analyst_buy_pct` range compression rationale (document or fix)

### Methodology decisions to document explicitly

- ☐ G-K4 — Bid-ask spread: document why real quotes (yfinance) used instead of paper's high-low proxy
- ☐ G-S8 — Divergence cap: either document as intentional engineering guardrail or remove (current asymmetric cap has no paper basis)
- ☐ G-K5 — Shrinkage factor in sub-index aggregation (`min(1, n/5)`) — document as engineering choice
- ☐ G-S10 — Redis-Postgres atomicity decision: accept self-healing or add retry

### Indefinite defer

- ☐ Regime adaptation (G-K8) — paper presents as advanced, real future work
- ☐ Dynamic weighting (rolling regression / IC max / Sharpe optimization) — major research project

### Recurring hygiene

- ☐ Quarterly methodology compliance re-audit (re-run audits A-D against current code)
- ☐ Quarterly signal catalog refresh (re-generate docs/SIGNAL_CATALOG.md)
- ☐ Monthly: review pipeline health alerts for "writer wrote 0 rows" patterns
- ☐ Per-merge: spot-check that fixes haven't introduced new methodology divergences

---

## Open decisions requiring your input

Things I can't decide for you that block progress:

1. **API breaking change strategy for `composite_score_smoothed`.** Once Phase 1.2 G-C3 lands, do you serve smoothed by default (breaking change for current API consumers) or as additional field (preserves compatibility but doesn't match paper)?

2. **Phase 1 deploy strategy.** All-at-once after the full Phase 1 sprint, or incrementally per-fix? Incremental is safer but means more deploy windows; all-at-once is bigger but cleaner.

3. **Phase 2 scope decisions.** Which Phase 2 items do you commit to before paper publication vs after?

4. **Validation timeline.** When do you want to start Phase 3? Before all of Phase 1 is done (validates partial), or after (validates final)?

5. **Local Postgres fate.** Drop, sync, or rename env vars?

6. **Audit E (validation infrastructure).** Run before or after Phase 1 fixes start?