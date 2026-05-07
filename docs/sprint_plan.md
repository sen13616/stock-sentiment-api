# Sprint Plan — SentimentAPI / SentientMarkets

**Status:** APPROVED

---

## Plan revision history

| Rev | Date | Author | Summary |
|-----|------|--------|---------|
| 0 | 2026-05-07 | Claude Code | Initial draft generated from masterchecklist.md |
| 1 | 2026-05-07 | Aayudh (review) + Claude Code (edits) | Sprint 2 reclassified Small/0.5d; Sprint 3 gets Q10–Q12; Sprint 4 audit re-runs moved to Sprint 7; Sprint 5 split into 5a/5b; Sprint 7 gains audit re-run deliverables; Sprint 17 split into 17a/17b; Open Question #9 resolved (Audit E between Sprint 7 and 17a); revision history section added |
| 2 | 2026-05-07 | Aayudh (decisions) + Claude Code (edits) | Q5, Q6, Q8 resolved. Plan status changed from DRAFT to APPROVED. Q1, Q11, Q12 remain open but are not blockers for Sprint 1 or Sprint 2 — will be resolved before Sprint 3 begins |

---

## Overview

This plan covers all items in the master checklist across Phases 1–4 and the maintenance backlog, organized into 24 sprints (after splits) plus non-sprint housekeeping items. The work divides into four arcs:

**Arc 1 (Sprints 1–7): Phase 1 — Foundational methodology compliance.** Quick-win math fixes first to establish a working rhythm, followed by the three architecture-level refactors (global scoring tick, z-score normalization, EMA smoothing), semantic dedup wiring, and a validation checkpoint. This is the critical path — everything downstream depends on the scoring methodology being correct.

**Arc 2 (Sprints 8–16): Phase 2 — Methodology depth.** The FinBERT NLP pipeline is the centerpiece, followed by smaller additions (language detection, ingestion frequency, ABSA, FRED macro signals, analyst rating dynamics, author credibility, macro stock-specificity, bootstrap confidence). Several Phase 2.3 signal source items are blocked on data source decisions and remain unscheduled until those decisions are made.

**Arc 3 (Sprints 17a–19): Phase 3 — Validation and publication.** Builds the backtesting and validation infrastructure, runs statistical tests, and produces the empirical evidence for the paper. Sprint 17a runs post-Phase-1 for early feedback; Sprint 17b re-runs post-Phase-2 with the full signal set. Paper writing itself is non-sprint work.

**Arc 4 (Sprints 20–22): Phase 4 + Maintenance — Productization and cleanup.** API productization, tooling/observability, and cosmetic code fixes from the audit backlog.

---

## Sprint catalog

### Sprint 1 — Phase 1.3 math corrections

- **Size:** Small
- **Branch:** `feature/sprint-1-math-corrections`
- **Phase of master checklist:** Phase 1.3
- **Items addressed:** G-S4 (log returns), G-S6 (NaN/Inf validation), G-S13 (AV news weight 1.0→0.7), G-S14 (min relevance threshold)
- **Decisions required before this sprint:** None — all resolved. G-S14 threshold = 0.1 (resolved Q5, rev-2).
- **Dependencies on prior sprints:** None (first sprint)
- **Expected files modified:**
  - `pipeline/sources/market.py` — `_compute_returns()`: change to `math.log(current/prev)` (G-S4)
  - `pipeline/features/normalize.py` — add `if math.isnan(value) or math.isinf(value): continue` guards in `score_market_signals()`, `score_narrative_signals()`, `score_influencer_signals()`, `score_macro_signals()` (G-S6); change `_SOURCE_WEIGHTS["alpha_vantage"]` from 1.0 to 0.7 (G-S13); add relevance floor filter in `score_narrative_signals()` (G-S14)
  - `tests/test_scoring.py`, `tests/test_new_scorers.py` — update/add tests for all four changes
- **Verification approach:** Full test suite pass; 10-ticker baseline comparison (all four fixes are behavior-changing)
- **Estimated duration:** 1–2 days
- **Risk level:** Low
- **One-sentence rationale:** Four independent, trivially small formula/constant changes (each 1–3 lines), batched for efficiency — spans Market and Narrative layers, but fixes are completely non-interacting, making cross-layer debugging confusion a non-issue.

### Sprint 2 — Market sub-index volume ratio (G-S3)

- **Size:** Small
- **Branch:** `feature/sprint-2-volume-ratio`
- **Phase of master checklist:** Phase 1.3
- **Items addressed:** G-S3
- **Decisions required before this sprint:** None — all resolved. Volume ratio = 6th component at weight 0.10, redistributed from returns (0.35→0.30) and order_flow (0.25→0.20) (resolved Q6, rev-2).
- **Dependencies on prior sprints:** None strict (sequenced after Sprint 1 for clean diffs)
- **Expected files modified:**
  - `pipeline/scoring/subindices.py` — add `"volume"` to `MARKET_COMPONENT_WEIGHTS`, add volume extraction logic in `compute_market_sub_index()`, update weight redistribution
  - `pipeline/features/normalize.py` — verify `_score_volume_ratio()` output is compatible
  - `tests/test_market_subindex.py` — update component weight tests, add volume component tests
- **Verification approach:** Full test suite; 10-ticker baseline comparison (market sub-index will change)
- **Estimated duration:** 0.5 days
- **Risk level:** Low
- **One-sentence rationale:** Single design decision (component structure) warrants its own sprint rather than overloading Sprint 1 with a decision-bearing item.

### Sprint 3 — Global scoring tick (G-C2)

- **Size:** Large
- **Branch:** `feature/sprint-3-scoring-tick`
- **Phase of master checklist:** Phase 1.2
- **Items addressed:** G-C2
- **Decisions required before this sprint:**
  - **Q10 (NEW):** Deploy/transition strategy — what happens between deploy and first scoring tick? Options: (a) pause old per-job scoring before deploy and accept a brief gap, (b) run old per-job scoring and new scoring tick in parallel during a transition window, (c) deploy new code with scoring tick active and let the first tick overwrite stale per-job scores.
  - **Q11 (NEW):** `_carry_forward_layer()` — remove entirely, or repurpose as a fallback when the scoring tick fails to produce a layer? If repurposed, define the failure conditions that trigger it.
  - **Q12 (NEW):** Rollback plan — if the first scoring tick produces anomalous data (e.g., all scores cluster at 50, or >20-point jumps vs pre-deploy baseline), what is the rollback procedure? Revert deploy? Manual DB correction? Automatic anomaly detection?
- **Dependencies on prior sprints:** Sprints 1–2 should be complete so math fixes are in place before architecture changes
- **Expected files modified:**
  - `pipeline/scheduler.py` — add `scoring_tick_job` (30-min cron); remove `_score_all()` calls from `market_job`, `narrative_job`, `influencer_job`, `macro_job`; keep `_score_all()` function but invoke from new tick job only
  - `pipeline/orchestrator.py` — refactor `_score_and_write()` to default `layers=None` (recompute all); handle `_carry_forward_layer()` per Q11 decision
  - `tests/test_scoring.py`, `tests/test_fallback.py`, `tests/test_scheduler_config.py` — update for new tick-based architecture
- **Verification approach:** Full test suite; 10-ticker baseline; production window: confirm `*_as_of` timestamps in `sentiment_history` align within seconds; confirm row count drops to ~48/ticker/day
- **Estimated duration:** 3–5 days (including production verification windows)
- **Risk level:** High
- **One-sentence rationale:** Architecture-level change that decouples data ingestion from scoring — highest-value fix, unblocks downstream normalization and smoothing work.

### Sprint 4 — Z-score normalization + per-source half-lives (G-C1 + G-S1)

- **Size:** Large
- **Branch:** `feature/sprint-4-zscore-normalization`
- **Phase of master checklist:** Phase 1.2
- **Items addressed:** G-C1, G-S1 (also subsumes maintenance items G-K2: short volume min obs 10→30, and G-K3: sigma floor 1e-12→1e-4)
- **Decisions required before this sprint:** None — z-score methodology defined in checklist
- **Dependencies on prior sprints:** Sprint 3 (global scoring tick must be stable before changing normalization)
- **Expected files modified:**
  - `pipeline/features/normalize.py` — implement `RollingZScorer` class; replace fixed parametric scorers `_score_return`, `_score_rsi`, `_score_volume_ratio`, `_score_order_flow_imbalance`, `_score_bid_ask_spread_bps` with z-score path; raise sigma floor from 1e-12 to 1e-4; update short volume window 20→90 and min obs 10→30; implement per-source half-lives in `_time_weight()`: market 60min, news 12h, analyst 3d, filings 7d, macro 14d
  - `pipeline/scoring/subindices.py` — update `compute_market_sub_index()` to consume z-scores instead of fixed-scale scores (resolves G-S2)
  - `db/queries/raw_signals.py` — update `get_signal_history()` to support 90-day lookback
  - `tests/test_scoring.py`, `tests/test_new_scorers.py`, `tests/test_short_volume_normalizer.py`, `tests/test_market_subindex.py` — substantial test updates
- **Verification approach:** Full test suite; 10-ticker baseline comparison (scores will shift significantly — expected 5–15 points)
- **Estimated duration:** 4–6 days (including baseline comparison)
- **Risk level:** High
- **One-sentence rationale:** The core math overhaul — replaces fixed parametric scorers with adaptive z-score normalization, the single largest methodology compliance fix.

### Sprint 5a — EMA smoothing (forward-only implementation) (G-C3)

- **Size:** Medium
- **Branch:** `feature/sprint-5a-ema-smoothing`
- **Phase of master checklist:** Phase 1.2
- **Items addressed:** G-C3 (forward-only EMA computation and persistence)
- **Decisions required before this sprint:** API contract — serve smoothed by default or as additional field? (Open decision #1 from master checklist)
- **Dependencies on prior sprints:** Sprint 4 (z-score normalization must be stable before adding smoothing on top)
- **Expected files modified:**
  - `migrations/006_add_smoothed_score.sql` — new file: add `composite_score_smoothed FLOAT` to `sentiment_history` (Aayudh runs migration manually)
  - `pipeline/orchestrator.py` — add EMA computation in `_score_and_write()` (after composite calc); pull previous smoothed value from Redis; formula: `alpha = 1 - 0.5^(dt/half_life)` with half_life=4h
  - `pipeline/persistence/redis_writer.py` — persist `composite_score_smoothed` in Redis state
  - `pipeline/persistence/pg_writer.py` — write smoothed value to new column
  - `db/queries/sentiment_history.py` — update `insert_row()` and `get_latest()` to include smoothed column
  - `api/response/assembler.py` — expose smoothed value (field placement depends on API contract decision)
  - `tests/` — new EMA logic tests, updated persistence tests
- **Verification approach:** Full test suite; 10-ticker baseline; production: verify smoothed scores populate and EMA behaves across multiple scoring cycles
- **Estimated duration:** 3–5 days (including multi-cycle verification)
- **Risk level:** Medium
- **One-sentence rationale:** Forward-only EMA implementation — adds temporal smoothing to eliminate score jitter for new data, completing the paper's core methodology requirements.

### Sprint 5b — EMA historical backfill

- **Size:** Medium
- **Branch:** `feature/sprint-5b-ema-backfill`
- **Phase of master checklist:** Phase 1.2
- **Items addressed:** G-C3 (historical backfill of smoothed scores)
- **Decisions required before this sprint:** API contract decision from Sprint 5a must be locked (determines which column is served as default)
- **Dependencies on prior sprints:** Sprint 5a (EMA implementation must be verified in production before backfilling historical data)
- **Expected files modified:**
  - `tools/backfill_ema.py` — new file: walk-forward EMA backfill script that reads `sentiment_history` rows chronologically per ticker and computes `composite_score_smoothed` retroactively
  - `db/queries/sentiment_history.py` — add `get_all_chronological(ticker)` and `update_smoothed_score(row_id, value)` queries
  - `tests/` — backfill script logic tests (verify walk-forward produces same results as live EMA)
- **Verification approach:** Run backfill on 10-ticker subset; verify backfilled smoothed scores match what live EMA would have produced; compare pre/post backfill API responses for historical endpoint
- **Estimated duration:** 2–3 days
- **Risk level:** Medium
- **One-sentence rationale:** Retroactively fills smoothed scores for historical data so the history API serves consistent EMA-smoothed values, not just forward-only.

### Sprint 6 — Wire semantic deduplication (G-C6)

- **Size:** Medium
- **Branch:** `feature/sprint-6-semantic-dedup`
- **Phase of master checklist:** Phase 1.3
- **Items addressed:** G-C6
- **Decisions required before this sprint:** None — `cluster_articles()` already exists in `pipeline/nlp/dedup.py`; this is a wiring sprint
- **Dependencies on prior sprints:** Sprint 3 (scoring tick architecture should be stable; narrative scoring flow may have changed)
- **Expected files modified:**
  - `pipeline/scheduler.py` — call `cluster_articles(ticker)` from `narrative_job()` after article ingestion, before the scoring tick picks it up
  - `db/queries/raw_articles.py` — modify `get_articles_since()` to use `DISTINCT ON (COALESCE(event_cluster_id, id::text))` selecting highest-relevance article per cluster
  - `tests/` — test that clustered articles are deduplicated in scoring path
- **Verification approach:** Full test suite; verify `event_cluster_id` populated for new articles post-deploy; verify narrative sub-index reflects deduplicated events
- **Estimated duration:** 1–2 days
- **Risk level:** Low
- **One-sentence rationale:** Wiring task connecting existing NLP clustering code (`pipeline/nlp/dedup.py`) to the live scoring pipeline — isolated integration with clear verification.

### Sprint 7 — Phase 1 behavior validation + audit re-runs

- **Size:** Medium
- **Branch:** `feature/sprint-7-phase1-validation`
- **Phase of master checklist:** Phase 1.4
- **Items addressed:** All Phase 1.4 items (capture baselines, document changes, tag release); audit re-runs (moved from Sprint 4)
- **Decisions required before this sprint:** Phase 1 deploy strategy (Open decision #2 — incremental vs all-at-once)
- **Dependencies on prior sprints:** All of Sprints 1–6 and 5b must be complete and verified
- **Deliverables:**
  - `tools/exports/phase1_baseline_post.json` — post-Phase-1 10-ticker baseline
  - `tools/exports/phase1_comparison.md` — comparison report (pre vs post)
  - `docs/audit_A_market_postphase1.md` — re-run of Audit A (market layer) comparing pre-Phase-1 to post-Phase-1 state, documenting methodology compliance
  - `docs/audit_D_influencer_macro_postphase1.md` — re-run of Audit D (influencer+macro layers) comparing pre-Phase-1 to post-Phase-1 state, documenting methodology compliance
  - `masterchecklist.md` — mark Phase 1 items as verified
- **Verification approach:** Side-by-side 10-ticker comparison; verify score changes within expected bounds (5–15 point composite shifts); document per-field deltas; **Phase 1 gate is contingent on audit re-runs showing methodology compliance** — if audits surface regressions, the gate does not pass until they are resolved
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** Dedicated validation checkpoint with audit re-runs before Phase 2 — confirms Phase 1 fixes produce expected behavior in aggregate and documents methodology compliance.

---

### Audit E — Validation infrastructure audit

- **Size:** Small
- **Branch:** `feature/audit-e-validation-infra`
- **Phase of master checklist:** Phase 3 (gate)
- **Items addressed:** Open Question #9 (resolved: Audit E runs between Sprint 7 and Sprint 17a)
- **Decisions required before this sprint:** None — this resolves the question
- **Dependencies on prior sprints:** Sprint 7 (Phase 1 gate must pass)
- **Deliverables:**
  - `docs/audit_E_validation_infra.md` — audit of validation infrastructure readiness: data availability, forward return computation feasibility, train/test split viability, statistical test prerequisites
- **Verification approach:** Document-based audit; confirms the data and infrastructure are sufficient to support Sprint 17a's validation work
- **Estimated duration:** 1 day
- **Risk level:** Low
- **One-sentence rationale:** Gate audit ensuring validation infrastructure is sound before investing in Sprint 17a — prevents building on a shaky foundation.

---

### Sprint 8 — FinBERT scoring (G-C5)

- **Size:** Large
- **Branch:** `feature/sprint-8-finbert`
- **Phase of master checklist:** Phase 2.1
- **Items addressed:** G-C5
- **Decisions required before this sprint:** Model variant (FinBERT-tone vs ProsusAI/FinBERT vs FinBERT-FLS); deployment mode (in-pipeline per article vs batched background job); new dependency authorization (`transformers`, `torch`)
- **Dependencies on prior sprints:** Sprint 7 (Phase 1 complete)
- **Expected files modified:**
  - `pipeline/nlp/finbert.py` — new file: model loading, inference, batch scoring
  - `pipeline/sources/narrative.py` — call FinBERT after article ingestion
  - `db/queries/raw_articles.py` — add `finbert_pos`, `finbert_neg`, `finbert_neu`, `finbert_score` columns
  - `migrations/007_add_finbert_columns.sql` — schema migration (Aayudh runs)
  - `requirements.txt` — add transformers/torch (requires authorization)
  - `tests/` — FinBERT scoring tests
- **Verification approach:** Unit tests for scoring logic; integration test with sample articles; verify `finbert_score` populated for new articles
- **Estimated duration:** 3–5 days
- **Risk level:** High
- **One-sentence rationale:** Largest Phase 2 item — adds local NLP inference, replacing reliance on provider sentiment scores.

### Sprint 9 — FinBERT pipeline integration (G-C4 + G-S11)

- **Size:** Medium
- **Branch:** `feature/sprint-9-finbert-integration`
- **Phase of master checklist:** Phase 2.1
- **Items addressed:** G-C4 (use FinBERT for Finnhub articles), G-S11 (wconf model confidence)
- **Decisions required before this sprint:** wconf formula choice: `max(P(k))` or `1 - H(P)`
- **Dependencies on prior sprints:** Sprint 8 (FinBERT model and class probabilities must exist)
- **Expected files modified:**
  - `db/queries/raw_articles.py` — modify `get_articles_since()` to use `COALESCE(finbert_score, provider_sentiment)` (G-C4)
  - `pipeline/features/normalize.py` — add `wconf` weight factor in `score_narrative_signals()` (G-S11)
  - `tests/` — verify Finnhub articles (previously NULL provider_sentiment) now score via FinBERT
- **Verification approach:** Full test suite; verify Finnhub articles contribute to narrative sub-index; compare narrative scores pre/post
- **Estimated duration:** 2–3 days
- **Risk level:** Medium
- **One-sentence rationale:** Connects FinBERT output to the scoring pipeline and adds model confidence weighting — natural follow-on to Sprint 8.

### Sprint 10 — Pipeline infrastructure updates (G-S16, G-S17, G-S18)

- **Size:** Small
- **Branch:** `feature/sprint-10-pipeline-infra`
- **Phase of master checklist:** Phase 2.1 (G-S16), Phase 2.3 (G-S17, G-S18)
- **Items addressed:** G-S16 (language detection), G-S17 (hourly macro ingestion), G-S18 (hourly EDGAR ingestion)
- **Decisions required before this sprint:** New dependency for language detection (`langdetect` or `fasttext`)
- **Dependencies on prior sprints:** Sprint 3 (scheduler architecture stable for frequency changes)
- **Expected files modified:**
  - `pipeline/sources/narrative.py` — add language detection at ingestion time (G-S16)
  - `db/queries/raw_articles.py` — filter to English-only articles in `get_articles_since()`
  - `migrations/008_add_language.sql` — add `language VARCHAR(10)` column (Aayudh runs)
  - `pipeline/scheduler.py` — change `macro_job` from daily CronTrigger (02:00 UTC) to hourly during trading hours (G-S17); separate `insider_job` from `influencer_job`, run insider hourly (G-S18)
  - `tests/test_scheduler_config.py` — update schedule assertions
- **Verification approach:** Full test suite; verify `language` column populated for new articles; verify scheduler fires at new frequencies
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** Three small pipeline configuration/filtering changes batched for efficiency — each touches distinct code paths with no interaction.

### Sprint 11 — ABSA (aspect-based sentiment)

- **Size:** Medium
- **Branch:** `feature/sprint-11-absa`
- **Phase of master checklist:** Phase 2.2
- **Items addressed:** Phase 2.2 ABSA items
- **Decisions required before this sprint:** Model approach (NER + per-aspect scoring vs ABSA-specific model); integration decision (wire into composite or keep as separate analytical layer)
- **Dependencies on prior sprints:** Sprint 8 (FinBERT provides model-serving infrastructure patterns)
- **Expected files modified:**
  - `pipeline/nlp/absa.py` — new file: aspect extraction and scoring
  - `db/queries/raw_articles.py` — schema: add `aspect_scores JSONB`
  - `migrations/` — schema migration (Aayudh runs)
  - `api/response/assembler.py` — expose aspect-level sentiment for Pro tier
  - `tests/` — ABSA extraction and scoring tests
- **Verification approach:** Unit tests; sample article aspect extraction; API response validation for Pro tier
- **Estimated duration:** 3–5 days
- **Risk level:** Medium
- **One-sentence rationale:** Self-contained NLP feature with clear schema, model, and API touchpoints.

### Sprint 12 — FRED macro signals (G-S20)

- **Size:** Medium
- **Branch:** `feature/sprint-12-fred-macro`
- **Phase of master checklist:** Phase 2.3
- **Items addressed:** G-S20 (interest rates + liquidity indicators)
- **Decisions required before this sprint:** None — FRED API is free, well-documented
- **Dependencies on prior sprints:** Sprint 3 (scoring tick architecture)
- **Expected files modified:**
  - `pipeline/sources/macro.py` — add FRED API calls for Treasury yields, Fed funds rate, TED spread, dollar liquidity; store under `_MACRO_` ticker convention
  - `pipeline/features/normalize.py` — add scorers for new macro signal types
  - `pipeline/rate_limits.py` — add FRED rate limit semaphore if needed
  - `tests/` — new macro signal tests
- **Verification approach:** Full test suite; verify new signals stored in `raw_signals`; verify macro sub-index incorporates new signals
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** New free data source with straightforward integration into existing macro pipeline.

### Sprint 13 — Analyst rating dynamics (G-S21)

- **Size:** Medium
- **Branch:** `feature/sprint-13-analyst-ratings`
- **Phase of master checklist:** Phase 2.3
- **Items addressed:** G-S21
- **Decisions required before this sprint:** None — uses existing Finnhub data, computes deltas from historical ratings
- **Dependencies on prior sprints:** None strict (sequenced after Phase 1 for stable base)
- **Expected files modified:**
  - `pipeline/sources/influencer.py` — store historical recommendation snapshots; compute upgrade/downgrade events
  - `db/queries/raw_signals.py` — query historical `analyst_buy_pct` for delta computation
  - `pipeline/features/normalize.py` — add scorer for `analyst_rating_change` signal type
  - `tests/` — rating change detection tests
- **Verification approach:** Full test suite; verify upgrade/downgrade events detected from simulated historical data
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** Enhances existing influencer pipeline with rating change detection using already-available Finnhub data.

### Sprint 14 — Author credibility (G-S12)

- **Size:** Medium
- **Branch:** `feature/sprint-14-author-credibility`
- **Phase of master checklist:** Phase 2.4
- **Items addressed:** G-S12
- **Decisions required before this sprint:** Credibility scheme (heuristic table, learned, or hybrid)
- **Dependencies on prior sprints:** Sprint 8 (FinBERT — for article processing patterns)
- **Expected files modified:**
  - `db/queries/raw_articles.py` — schema: add `author VARCHAR(200)`, `author_credibility FLOAT` columns
  - `migrations/` — schema migration (Aayudh runs)
  - `pipeline/sources/narrative.py` — extract author at ingestion time
  - `pipeline/sources/influencer.py` — extract `insider_role` from Form 4 XML (CEO, VP, board member)
  - `pipeline/features/normalize.py` — add `wauthor` weight in narrative event-level weighting
  - `tests/` — credibility weighting tests
- **Verification approach:** Full test suite; verify author fields populated; verify credibility weight affects narrative scores directionally
- **Estimated duration:** 2–3 days
- **Risk level:** Medium
- **One-sentence rationale:** Cross-cutting feature touching narrative and influencer pipelines — grouped because both involve author/insider identity.

### Sprint 15 — Macro stock-specificity (G-K10)

- **Size:** Medium
- **Branch:** `feature/sprint-15-macro-specificity`
- **Phase of master checklist:** Phase 2.5
- **Items addressed:** G-K10
- **Decisions required before this sprint:** None — `ticker_universe.sector` mapping partially exists
- **Dependencies on prior sprints:** Sprint 3 (scoring tick); Sprint 12 (FRED signals enrich macro sub-index)
- **Expected files modified:**
  - `pipeline/orchestrator.py` — modify macro scoring to accept ticker parameter and use per-ticker sector ETF weight instead of uniform basket
  - `pipeline/scoring/subindices.py` — update macro sub-index computation to weight sector ETFs by ticker's sector
  - `db/queries/universe.py` — add query for ticker→sector mapping
  - `tests/` — verify different-sector tickers produce different macro sub-indices
- **Verification approach:** Full test suite; verify tech tickers (AAPL) weight XLK more heavily than healthcare tickers (ABBV) weight XLK
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** Makes macro sub-index stock-specific using existing sector mapping data — isolated change with clear verification.

### Sprint 16 — Bootstrap confidence intervals (G-S9)

- **Size:** Medium
- **Branch:** `feature/sprint-16-bootstrap-confidence`
- **Phase of master checklist:** Phase 2.6
- **Items addressed:** G-S9
- **Decisions required before this sprint:** None — methodology defined in checklist
- **Dependencies on prior sprints:** Sprint 4 (z-score normalization provides signal distributions for bootstrap)
- **Expected files modified:**
  - `pipeline/confidence/bootstrap.py` — new file: bootstrap sampling and interval computation
  - `pipeline/confidence/scorer.py` — integrate bootstrap intervals alongside operational confidence
  - `db/queries/sentiment_history.py` — add `confidence_interval_low`, `confidence_interval_high` columns
  - `migrations/` — schema migration (Aayudh runs)
  - `api/response/assembler.py` — expose confidence intervals for Pro tier
  - `tests/` — bootstrap interval tests
- **Verification approach:** Full test suite; verify intervals are reasonable (not too wide, not zero-width); operational confidence preserved
- **Estimated duration:** 2–3 days
- **Risk level:** Medium
- **One-sentence rationale:** Adds statistical confidence alongside operational confidence — needed for the validation chapter of the paper.

---

### Sprint 17a — Post-Phase-1 validation

- **Size:** Large
- **Branch:** `feature/sprint-17a-validation-phase1`
- **Phase of master checklist:** Phase 3.1
- **Items addressed:** Forward returns construction, train/test split, backtesting harness, IC computation, decile portfolio construction — run against post-Phase-1 signal set
- **Decisions required before this sprint:** Train/test split methodology; forward return horizons (1d, 5d, 20d per checklist)
- **Dependencies on prior sprints:** Sprint 7 (Phase 1 gate must pass); Audit E (validation infrastructure audit must confirm data sufficiency)
- **Expected files modified:**
  - `tools/validation/` — new directory: `forward_returns.py`, `backtester.py`, `ic_computation.py`, `portfolio.py`
  - `db/queries/` — new queries for historical returns and sentiment lookback
  - `tools/exports/validation_phase1_results.json` — Phase 1 validation results
  - `tests/` — validation logic tests
- **Verification approach:** Run on historical data; confirm IC values are directionally sensible; confirm portfolio construction produces non-degenerate results; document Phase 1 signal-set performance as baseline for Phase 2 comparison
- **Estimated duration:** 4–6 days
- **Risk level:** Medium
- **One-sentence rationale:** Early validation feedback on Phase 1 signals — establishes baseline IC/portfolio performance before Phase 2 enrichment, enabling before/after comparison.

### Sprint 17b — Post-Phase-2 validation (re-run)

- **Size:** Medium
- **Branch:** `feature/sprint-17b-validation-phase2`
- **Phase of master checklist:** Phase 3.1
- **Items addressed:** Re-run of Sprint 17a validation suite against the post-Phase-2 enriched signal set
- **Decisions required before this sprint:** None — uses infrastructure from Sprint 17a
- **Dependencies on prior sprints:** Sprint 16 (Phase 2 complete); Sprint 17a (validation infrastructure must exist)
- **Expected files modified:**
  - `tools/exports/validation_phase2_results.json` — Phase 2 validation results
  - `tools/exports/validation_phase1_vs_phase2_comparison.md` — comparison report showing IC/portfolio improvement from Phase 2 signals
- **Verification approach:** Compare Phase 2 IC values against Phase 1 baseline from Sprint 17a; document improvement (or lack thereof) per signal addition; identify which Phase 2 signals contributed most to IC improvement
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** Re-runs the same validation infrastructure with the full Phase 2 signal set — measures the marginal value of each Phase 2 addition.

### Sprint 18 — Statistical validation tests (Phase 3.1)

- **Size:** Medium
- **Branch:** `feature/sprint-18-statistical-tests`
- **Phase of master checklist:** Phase 3.1
- **Items addressed:** Lead-lag analysis, Granger causality tests, event studies
- **Decisions required before this sprint:** Event study design (which events — earnings, FDA approvals, etc.?)
- **Dependencies on prior sprints:** Sprint 17a (validation infrastructure)
- **Expected files modified:**
  - `tools/validation/lead_lag.py` — lead-lag analysis
  - `tools/validation/granger.py` — Granger causality tests
  - `tools/validation/event_study.py` — event study framework
  - `tests/` — statistical test correctness checks
- **Verification approach:** Run on historical data; verify statistical significance levels; spot-check against known events
- **Estimated duration:** 3–4 days
- **Risk level:** Low
- **One-sentence rationale:** Statistical tests that build on Sprint 17a's infrastructure but are methodologically independent of each other.

### Sprint 19 — Ablation and robustness (Phase 3.1)

- **Size:** Medium
- **Branch:** `feature/sprint-19-ablation-robustness`
- **Phase of master checklist:** Phase 3.1
- **Items addressed:** Sub-index ablations, regime-conditional performance, baseline comparisons, robustness checks
- **Decisions required before this sprint:** Baseline model choices (price-only? momentum-only? naive sentiment?)
- **Dependencies on prior sprints:** Sprint 17a (validation infrastructure); Sprint 18 (statistical tests for comparison)
- **Expected files modified:**
  - `tools/validation/ablation.py` — sub-index ablation framework
  - `tools/validation/regime.py` — regime-conditional analysis (high-vol vs low-vol, bull vs bear)
  - `tools/validation/baselines.py` — baseline model implementations
  - `tools/validation/robustness.py` — parameter sensitivity analysis
  - `tests/` — ablation and comparison tests
- **Verification approach:** Verify ablation results are directionally sensible (removing a layer should degrade IC); verify robustness across parameter variations
- **Estimated duration:** 3–4 days
- **Risk level:** Low
- **One-sentence rationale:** Final validation layer — ablations and robustness checks that require the full validation pipeline from Sprints 17a–18.

---

### Sprint 20 — API productization (Phase 4.2)

- **Size:** Medium
- **Branch:** `feature/sprint-20-api-productization`
- **Phase of master checklist:** Phase 4.2
- **Items addressed:** Rate limiting verification, API documentation, `/sentiment/{ticker}/history` enhancements, public changelog
- **Decisions required before this sprint:** Pro tier billing integration approach
- **Dependencies on prior sprints:** Sprint 5a (EMA smoothing — API serves smoothed scores); Sprint 7 (Phase 1 complete)
- **Expected files modified:**
  - `api/rate_limit.py` — verify implementation matches stated limits (10 req/min free, 120 req/min pro)
  - `api/routes/history.py` — configurable lookback for premium charts
  - `docs/API.md` — new file: API documentation
  - `CHANGELOG.md` — new file: public-facing changelog for Phase 1
- **Verification approach:** Rate limit integration tests; API documentation review; manual endpoint testing
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** Groups API-facing productization work that doesn't affect scoring methodology.

### Sprint 21 — Tooling and observability (Phase 4.3)

- **Size:** Small
- **Branch:** `feature/sprint-21-tooling`
- **Phase of master checklist:** Phase 4.3
- **Items addressed:** Pipeline health "0 rows" alarm, signal catalog generation, methodology compliance dashboard
- **Decisions required before this sprint:** None
- **Dependencies on prior sprints:** Sprint 7 (Phase 1 complete — signal catalog documents post-Phase-1 signals)
- **Expected files modified:**
  - `tools/health_check.py` — new file: pipeline health with "0 rows written" detection
  - `tools/signal_catalog.py` — new file: auto-generate `docs/SIGNAL_CATALOG.md`
  - `tools/compliance_dashboard.py` — new file: run audits and report drift
  - `docs/SIGNAL_CATALOG.md` — generated output
- **Verification approach:** Run tools manually; verify signal catalog matches codebase; verify health check detects simulated 0-row scenarios
- **Estimated duration:** 1–2 days
- **Risk level:** Low
- **One-sentence rationale:** Three independent tooling scripts batched for efficiency.

### Sprint 22 — Maintenance cosmetic fixes

- **Size:** Medium
- **Branch:** `feature/sprint-22-maintenance`
- **Phase of master checklist:** Maintenance backlog
- **Items addressed:** G-K1 (RSI dual-scoring cleanup), G-K9 (persist cap_applied flag), G-K11 (sector_etf_close usage decision), G-K12 (analyst_buy_pct range compression rationale)
- **Decisions required before this sprint:** G-K1: remove unused contrarian scorer or document dual-use; G-K11: drop `sector_etf_close` or use it (Sprint 15 may resolve this — if macro becomes stock-specific, sector ETF data has a clear purpose); G-K12: document compression rationale or widen range
- **Dependencies on prior sprints:** Sprint 15 (G-K10 macro specificity resolves whether sector_etf_close has a use); Sprint 4 (z-score normalization may change RSI scoring landscape for G-K1)
- **Expected files modified:**
  - `pipeline/features/normalize.py` — G-K1 (RSI scorer cleanup)
  - `pipeline/scoring/divergence.py` — G-K9 (persist cap_applied)
  - `db/queries/sentiment_history.py` — G-K9 (add `cap_applied BOOLEAN` column)
  - `migrations/` — G-K9 schema migration (Aayudh runs)
  - `pipeline/features/normalize.py` — G-K12 (`_score_analyst_buy_pct`)
- **Verification approach:** Full test suite; minimal behavior change expected
- **Estimated duration:** 2–3 days
- **Risk level:** Low
- **One-sentence rationale:** Low-priority cosmetic fixes batched after core methodology work is complete, when earlier sprints have resolved the decisions these items depend on.

---

## Non-sprint items

### Documentation cleanup (batch task, no branch needed)

Single-line or paragraph-level documentation changes that don't warrant sprint overhead. Handle as one commit: `docs: methodology documentation cleanup`.

| Item | Action |
|------|--------|
| G-K7 | Update CLAUDE.md composite weights from stale 0.4/0.25/0.2/0.15 to actual 0.35/0.30/0.25/0.10 |
| G-K4 | Document why real yfinance quotes used for bid-ask instead of paper's high-low proxy |
| G-S8 | Document divergence cap (`composite.py` line 68) as intentional engineering guardrail, or remove if unwanted |
| G-K5 | Document shrinkage factor `min(1, n/5)` in `subindices.py` as engineering choice |
| G-S10 | Document Redis-Postgres atomicity decision: accept self-healing or add retry |

### Phase 3.2 — Paper finalization (non-sprint, Aayudh-led)

Paper writing is not Code work. Checklist items tracked for reference:
- Update Methodology section to reflect actual Phase 1 implementation
- Document Phase 2 enhancements as future work
- Write Validation Methodology chapter using actual results
- Add limitations section
- Internal review → SSRN → journal submission

### Recurring hygiene (not sprint-able)

- Quarterly methodology compliance re-audit (re-run audits A–D)
- Quarterly signal catalog refresh (re-generate `docs/SIGNAL_CATALOG.md`)
- Monthly pipeline health review (check for "0 rows" patterns)
- Per-merge methodology divergence spot-check

---

## Sequencing rationale

The plan follows a strict dependency chain for Phase 1: math corrections first (Sprints 1–2) establish correct formulas before the architecture refactors (Sprints 3–5a) change how those formulas are orchestrated. Within architecture, the global scoring tick (Sprint 3) must be stable before z-score normalization (Sprint 4) replaces the scoring functions, and z-score must be stable before EMA smoothing (Sprint 5a) is layered on top. Sprint 5b (EMA backfill) runs only after 5a is verified in production and the API contract decision is locked. Semantic dedup (Sprint 6) is sequenced after the scoring tick because the narrative scoring flow changes in Sprint 3, but has no strict dependency on z-score or EMA.

Phase 2 is sequenced serially for practical reasons: one Code session at a time, and each sprint's findings may affect downstream work. The FinBERT pipeline (Sprints 8–9) is the natural first Phase 2 work because it's the largest single capability addition and other NLP features (ABSA, language detection) build on its infrastructure patterns. Pipeline infrastructure (Sprint 10) and signal-source expansions (Sprints 12–13) are interleaved with the NLP work.

Validation is split into two passes: Sprint 17a runs after Phase 1 (gated by Audit E) to provide early empirical feedback on the base signal set, and Sprint 17b re-runs after Phase 2 to measure the marginal value of each Phase 2 addition. This split enables a before/after comparison in the paper.

**Critical path:** Sprint 1 → Sprint 3 → Sprint 4 → Sprint 5a → Sprint 7 → Audit E → Sprint 17a (Phase 1 validation). Phase 2 path: Sprint 8 → Sprint 16 → Sprint 17b (Phase 2 validation).

---

## Checkpoints

**Checkpoint 1 — After Sprint 7 (Phase 1 complete).** The most important pause point. Evaluate: are scores behaving as expected? Are architecture changes stable in production? Do audit re-runs (audit_A and audit_D) confirm methodology compliance? Is methodology confidence sufficient to proceed to Phase 2? Should any Phase 1 fixes be revisited?

**Checkpoint 2 — After Sprint 9 (FinBERT pipeline complete).** Evaluate: does local NLP meaningfully improve narrative scoring versus provider sentiment? Are inference times acceptable for production? This determines investment in ABSA (Sprint 11) and shapes the paper's NLP narrative.

**Checkpoint 3 — After Sprint 16 (Phase 2 complete).** Evaluate: is the signal set rich enough for meaningful validation? Should any blocked Phase 2.3 items (earnings transcripts, Reddit, etc.) be unblocked before proceeding to validation? Final go/no-go for Sprint 17b.

**Checkpoint 4 — After Sprint 19 (validation complete).** Evaluate: are empirical results strong enough for publication? What's the paper narrative? Are additional signals or methodology changes needed, or is the current system sufficient? Compare Sprint 17a (Phase 1) vs Sprint 17b (Phase 2) results.

---

## Open questions

Decisions that affect the plan's shape. Should be resolved before the sprint that depends on them:

| # | Question | Blocks | Recommendation |
|---|----------|--------|----------------|
| 1 | API contract for `composite_score_smoothed`: serve smoothed by default (breaking change) or as additional field? | Sprint 5a | Serve smoothed; expose raw as `composite_score_raw` |
| 2 | Phase 1 deploy strategy: incremental per-sprint or all-at-once? | Sprint 7 | Incremental — each sprint is independently deployable |
| 3 | Phase 2 scope before paper: which Phase 2 items must complete before validation? | Sprint 17b timing | Minimum: FinBERT (Sprints 8–9). Everything else can be future work |
| 4 | ~~Validation timeline: start after Phase 1 only, or wait for Phase 2?~~ | ~~Sprint 17~~ | **RESOLVED (rev-1):** Split into 17a (post-Phase-1) and 17b (post-Phase-2). Both run. |
| 5 | ~~G-S14 relevance threshold: 0.1 or 0.2?~~ | ~~Sprint 1~~ | **RESOLVED (rev-2):** 0.1 (conservative starting value). Revisit after 30 days of production data. |
| 6 | ~~G-S3 volume ratio: 6th component or merge into existing?~~ | ~~Sprint 2~~ | **RESOLVED (rev-2):** 6th component at weight 0.10, redistributed from returns (0.35→0.30) and order_flow (0.25→0.20). |
| 7 | Signal source expansion decisions: data sources for earnings transcripts, Reddit, Twitter, G-S19, G-S22 | Unscheduled sprints | Resolve per-source when ready; these remain unscheduled |
| 8 | ~~Local Postgres fate: drop, sync, or rename env vars?~~ | ~~Non-blocking~~ | **RESOLVED (rev-2):** Drop it. Local Postgres has no use case (backtesting will run against production). Aayudh will handle cleanup separately (stop the service, remove the database, update .env to point only to production). Environmental work, not sprint work. |
| 9 | ~~Audit E timing: run validation infrastructure audit before or after Phase 1?~~ | ~~Sprint 17~~ | **RESOLVED (rev-1):** Audit E runs between Sprint 7 and Sprint 17a. It is the gate to validation work. |
| 10 | Sprint 3 deploy/transition strategy: what happens between deploy and first scoring tick? | Sprint 3 | Options: (a) pause old scoring, (b) run in parallel during transition, (c) deploy and let first tick overwrite |
| 11 | `_carry_forward_layer()`: remove entirely or repurpose as fallback when scoring tick fails to produce a layer? | Sprint 3 | — |
| 12 | Sprint 3 rollback plan: procedure if first scoring tick produces anomalous data | Sprint 3 | — |

---

## Out of scope

| Item | Reason |
|------|--------|
| G-K8 (Regime adaptation) | Indefinite defer per checklist — advanced future work |
| Dynamic weighting (rolling regression / IC max / Sharpe) | Indefinite defer per checklist — major research project |
| Phase 4.1 (SentientMarkets website) | Separate repo — outside sprint scope per sprintrules.md |
| Domain migration (Namecheap + Cloudflare) | Aayudh-handled, non-Code work |
| Direct production DB modifications | Aayudh runs manually per sprintrules.md |
| Schema migrations (execution) | Aayudh runs manually — sprints produce `.sql` files but don't execute them |
| Railway deployment configuration | Outside scope per sprintrules.md |
| API key rotation / `.env` changes | Outside scope per sprintrules.md |
| Earnings call transcripts (Phase 2.3) | Blocked on data source decision — will be scheduled when resolved |
| Reddit / forum sentiment (Phase 2.3) | Blocked on data source decision |
| Social media / Twitter (Phase 2.3) | Blocked on API access decision |
| G-S19 (Earnings estimate updates) | Blocked on data source (Finnhub paid? Alternative?) |
| G-S22 (Analyst target price unblocking) | Blocked on data source (Finnhub paid? Alternative?) |
| Recurring hygiene (quarterly audits, monthly reviews) | Not sprint-able — ongoing maintenance |
