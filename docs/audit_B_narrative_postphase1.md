# Audit B — Narrative Sub-Index Post-Phase-1 Re-Run

**Date:** 2026-05-10
**Original audit:** 2026-05-07
**Auditor:** Claude Code
**Sprints deployed since original:** 1, 2, 3, 4, 5a, 6

---

## Summary

The original Audit B (2026-05-07) identified 12 gaps in the narrative sub-index pipeline: 3 critical, 7 significant, 2 cosmetic. Sprints 1 through 6 addressed four of these gaps directly. Two critical gaps (C1/C2 — Finnhub unscored, no local NLP) have been reclassified as Accepted Phase 1 Simplifications: AV's provider_sentiment serves as the functioning external NLP model for Phase 1; Phase 2 will bring NLP in-house via FinBERT. The remaining open items are either blocked by Phase 2 prerequisites or represent deliberate Phase 1 scope boundaries.

**Status counts:**
- RESOLVED: 4 (G-S3, G-S4, G-S5, G-C3)
- ACCEPTED PHASE 1 SIMPLIFICATION: 2 (G-C1, G-C2)
- ACCEPTED (intentional deviation): 1 (G-K1)
- OPEN (Phase 2+): 5 (G-S1, G-S2, G-S6, G-S7, G-K2)
- REGRESSIONS: 0

---

## Gap Resolution Table

| Original ID | Gap Description | Original Severity | Sprint Fix | Current Status | Evidence |
|---|---|---|---|---|---|
| G-C1 (was C4 in tracker) | Finnhub articles ingested but never scored | Critical | -- | ACCEPTED PHASE 1 SIMPLIFICATION | Finnhub articles stored with `provider_sentiment=None` (narrative.py:186). `get_articles_since()` filters `provider_sentiment IS NOT NULL` (raw_articles.py:128). Phase 1 uses AV's external NLP model; Phase 2 will add FinBERT to score Finnhub articles via `COALESCE(finbert_score, provider_sentiment)`. |
| G-C2 (was C5 in tracker) | No local NLP model | Critical | -- | ACCEPTED PHASE 1 SIMPLIFICATION | All sentiment scoring uses AV's `ticker_sentiment_score` (normalize.py:437–449). AV's provider_sentiment is a functioning external NLP model that outputs scores in [-1, +1], mapped to [0, 100] at normalize.py:449. Phase 1 implements the external-API stage of the paper's progression. Phase 2 brings NLP in-house via FinBERT (`finbert_score` column exists in schema, `pipeline/nlp/finbert.py` planned). |
| G-C3 (was C6 in tracker) | Semantic dedup implemented but not wired | Critical | Sprint 6 | RESOLVED | **Verified.** Three changes confirm full wiring: (1) `cluster_articles()` is imported in scheduler.py:55 and called per-ticker in `narrative_job()` at scheduler.py:397. (2) `get_articles_since()` at raw_articles.py:119-138 uses `DISTINCT ON (COALESCE(event_cluster_id, id::text))` with `ORDER BY ... relevance_score DESC NULLS LAST` to select only the highest-relevance article per cluster. (3) The `WHERE` clause includes `provider_sentiment IS NOT NULL` (raw_articles.py:128) before the DISTINCT ON, so Finnhub articles with NULL sentiment are excluded from cluster selection entirely. Telemetry logging added at scheduler.py:407-419 tracks cross-source vs same-source cluster counts. |
| G-S1 | Model confidence weighting (`wconf`) missing | Significant | -- | OPEN (blocked by Phase 2) | AV returns only a scalar `ticker_sentiment_score`, not class probabilities (pos/neg/neutral). `wconf` requires probability distributions to compute inverse entropy or max class probability. Blocked until Phase 2 FinBERT provides class-level outputs. |
| G-S2 | Author credibility weighting (`wauthor`) missing | Significant | -- | OPEN (Phase 2+) | No author information is stored or used in scoring. All articles from the same source receive the same `wsrc` weight regardless of author. No schema column for author metadata. |
| G-S3 | 48h half-life for news (paper says 6-24h) | Significant | Sprint 4 | RESOLVED | **Verified.** Per-source half-lives implemented in normalize.py:60-78. `_LAYER_HALF_LIFE_H["narrative"] = 12.0` (normalize.py:62). The `_get_half_life(layer, source)` function at normalize.py:73-78 returns 12h for all narrative sources unless overridden. `score_narrative_signals()` at normalize.py:450 calls `_get_half_life("narrative", source)` and passes the result to `_time_weight()` at normalize.py:451. 12 hours is within the paper's 6-24h range for news. |
| G-S4 | AV source weight 1.0 (paper says 0.6-0.8 for news) | Significant | Sprint 1 | RESOLVED | **Verified.** `_SOURCE_WEIGHTS["alpha_vantage"] = 0.7` at normalize.py:42. This is within the paper's 0.6-0.8 range for "News (Reuters, Bloomberg)". Finnhub remains at 0.8 (normalize.py:45), which is at the top of the news range. |
| G-S5 | No relevance exclusion threshold | Significant | Sprint 1 | RESOLVED | **Verified.** `score_narrative_signals()` at normalize.py:444 now contains `if relevance < 0.1: continue`, which skips articles with relevance below 0.1. This replaces the old floor-at-0.1 approach (which allowed all articles through at 10% weight) with a hard exclusion gate. Articles with `relevance_score=None` default to 0.5 (normalize.py:443: `float(art.get("relevance_score") or 0.5)`), so they pass the threshold. |
| G-S6 | Narrative sources incomplete (4 of 6 paper types missing) | Significant | -- | OPEN (Phase 2+) | Only news articles from AV + Finnhub are ingested. Missing: earnings call transcripts, blogs, forums, social media. No changes since original audit. |
| G-S7 | Language detection absent | Significant | -- | OPEN (Phase 2+) | No language detection code exists. Non-English articles are scored by AV's model and contribute to the sub-index. No changes since original audit. |
| G-K1 | Shrinkage factor in sub-index aggregation (not in paper) | Cosmetic | -- | ACCEPTED (intentional) | `compute_sub_index()` at subindices.py:72-74 applies `shrinkage = min(1.0, n / 5)` to pull toward neutral with fewer than 5 articles. This is a deliberate engineering addition to prevent single-article overconfidence. Documented in code docstring at subindices.py:10-11. |
| G-K2 | Relevance default of 0.5 for articles without relevance score | Cosmetic | -- | OPEN (minor) | normalize.py:443: `float(art.get("relevance_score") or 0.5)`. Finnhub articles have `relevance_score=None` and default to 0.5. Currently moot because Finnhub articles have `provider_sentiment=None` and are excluded before this line. Will become relevant in Phase 2 when FinBERT scores Finnhub articles. |

---

## Detailed Findings (changed status only)

### G-C1: Finnhub articles unscored --> ACCEPTED PHASE 1 SIMPLIFICATION

**Original status:** Critical
**New status:** Accepted Phase 1 Simplification

The original audit correctly identified that Finnhub articles are stored with `provider_sentiment=None` (narrative.py:186) and excluded from scoring by the `provider_sentiment IS NOT NULL` filter in `get_articles_since()` (raw_articles.py:128). This remains true in the current code.

**Reclassification rationale:** Phase 1 of the pipeline implements the external-API stage of the paper's NLP progression. Alpha Vantage's NEWS_SENTIMENT API provides a functioning external NLP model that outputs per-ticker sentiment scores. This is a valid first stage. Phase 2 will add FinBERT (`pipeline/nlp/finbert.py`) to score all articles locally, at which point Finnhub articles will contribute via `COALESCE(finbert_score, provider_sentiment)`. The schema is already prepared: `finbert_score` column exists in `raw_articles` (currently NULL for all rows).

**Risk accepted:** Until Phase 2, if AV returns no articles for a ticker (rate limit, API error, no coverage), the narrative sub-index receives zero textual events even if Finnhub articles exist. This is mitigated by the fallback mechanism (`_fallback_subindex` in orchestrator.py:230) which recovers the last known narrative score from Redis within a 6-hour staleness window.

---

### G-C2: No local NLP model --> ACCEPTED PHASE 1 SIMPLIFICATION

**Original status:** Critical
**New status:** Accepted Phase 1 Simplification

All sentiment scoring relies on AV's pre-computed `ticker_sentiment_score` at normalize.py:437: `sentiment = art.get("provider_sentiment")`, mapped to [0, 100] at normalize.py:449: `score = max(0.0, min(100.0, 50.0 + 50.0 * sent_f))`.

**Reclassification rationale:** The paper describes a progression from lexicon-based to transformer-based models. Phase 1 uses an external NLP provider (AV) as an intermediate stage. AV's model produces scores in [-1, +1] that are format-compatible with the paper's `S_i = P(pos) - P(neg)`. Phase 2 will bring NLP in-house via FinBERT, enabling:
- Local control over the model pipeline
- Access to class probabilities (enabling `wconf` -- see G-S1)
- Scoring of Finnhub articles (resolving G-C1 fully)
- Potential for aspect-based sentiment analysis (ABSA)

---

### G-C3: Semantic dedup not wired --> RESOLVED (Sprint 6)

**Original status:** Critical
**New status:** Resolved

The original audit found that `cluster_articles()` was never called and `get_articles_since()` did not filter by `event_cluster_id`. Both issues are now fixed.

**Evidence of resolution:**

1. **`cluster_articles()` is called from `narrative_job()`:**
   - Import at scheduler.py:55: `from pipeline.nlp.dedup import cluster_articles`
   - Phase 2 of `narrative_job()` at scheduler.py:395-403 iterates all tickers and calls `cluster_articles(ticker)` after the fetch phase completes:
     ```python
     for ticker in tickers:
         try:
             n_clusters = await cluster_articles(ticker)
             total_clusters += n_clusters
             ...
     ```
   - The two-phase design (fetch first, cluster second) ensures all newly ingested articles are available for clustering.

2. **`get_articles_since()` uses DISTINCT ON with cluster-aware dedup:**
   - raw_articles.py:119-138 implements the complete dedup query:
     ```sql
     SELECT DISTINCT ON (COALESCE(event_cluster_id, id::text))
            published_at, provider_sentiment, relevance_score, source
     FROM raw_articles
     WHERE ticker               = $1
       AND published_at        >= $2
       AND provider_sentiment IS NOT NULL
     ORDER BY COALESCE(event_cluster_id, id::text),
              relevance_score DESC NULLS LAST,
              published_at DESC
     ```
   - Clustered articles: `DISTINCT ON (event_cluster_id)` selects only the highest-relevance article per cluster.
   - Unclustered articles (singletons): `COALESCE(event_cluster_id, id::text)` treats each unclustered article as its own unique group, so no data is lost.
   - `provider_sentiment IS NOT NULL` is in the WHERE clause (before DISTINCT ON), so Finnhub articles with NULL sentiment never participate in cluster selection -- the highest-relevance article chosen per cluster is always one that can be scored.

3. **Telemetry:**
   - `_get_cluster_telemetry()` at scheduler.py:67-98 queries the DB for cross-source vs same-source cluster breakdown after clustering completes.
   - Results are logged at scheduler.py:407-419 with fields: `total_articles_processed`, `total_clusters_formed`, `cross_source_clusters`, `same_source_clusters`, `largest_cluster_size`.

---

### G-S3: 48h half-life for news --> RESOLVED (Sprint 4)

**Original status:** Significant
**New status:** Resolved

**Evidence of resolution:**

The uniform 48h half-life has been replaced with per-source half-lives (Sprint 4, G-S1 in master checklist).

- `_LAYER_HALF_LIFE_H` at normalize.py:60-65 defines per-layer defaults:
  ```python
  _LAYER_HALF_LIFE_H = {
      "market":     1.0,      # 60 min
      "narrative":  12.0,     # 12 hours
      "influencer": 72.0,     # 3 days
      "macro":      336.0,    # 14 days
  }
  ```
- `_get_half_life(layer, source)` at normalize.py:73-78 resolves the half-life for any (layer, source) pair, with override support for special cases.
- `score_narrative_signals()` at normalize.py:450-451 now uses the layer-aware half-life:
  ```python
  half_life = _get_half_life("narrative", source)
  w_time = _time_weight(published, now, half_life)
  ```
- The narrative half-life of 12h is within the paper's 6-24h range for "News / Media".

**Decay comparison (12h vs original 48h, for a 3-day lookback window):**

| Article age | Weight at 12h half-life | Weight at 48h half-life (old) |
|---|---|---|
| 6 hours | 70.7% | 91.7% |
| 12 hours | 50.0% | 84.1% |
| 24 hours | 25.0% | 70.7% |
| 48 hours | 6.3% | 50.0% |
| 72 hours | 1.6% | 35.4% |

At 12h half-life, a 3-day-old article contributes only 1.6% weight -- effectively negligible, consistent with the paper's intent.

---

### G-S4: AV source weight 1.0 --> RESOLVED (Sprint 1)

**Original status:** Significant
**New status:** Resolved

**Evidence of resolution:**

normalize.py:42: `"alpha_vantage": 0.7`

The original value was 1.0, placing AV in the "Filings / official disclosures" tier. The corrected value of 0.7 is within the paper's "News (Reuters, Bloomberg)" tier of 0.6-0.8. Since AV is a news aggregator (not an official filing source), 0.7 is the appropriate placement.

---

### G-S5: No relevance exclusion threshold --> RESOLVED (Sprint 1)

**Original status:** Significant
**New status:** Resolved

**Evidence of resolution:**

normalize.py:443-445:
```python
relevance = float(art.get("relevance_score") or 0.5)
if relevance < 0.1:
    continue
```

The original code floored relevance at 0.1 in the weight computation (`max(relevance, 0.1)`) but never excluded articles. Now, articles with relevance below 0.1 are hard-excluded before scoring. This implements the paper's "relevance filtering to separate stock-specific information from broader market noise."

**Note:** The threshold of 0.1 is conservative -- it excludes only the least-relevant articles. Articles without a relevance score (e.g., Finnhub) default to 0.5 and pass the gate. This is currently moot for Finnhub since their articles have `provider_sentiment=None` and are excluded earlier, but will become relevant in Phase 2.

---

## Findings Unchanged

### G-S1: Model confidence weighting (`wconf`) missing -- OPEN

Blocked by Phase 2 FinBERT. AV returns only a scalar score, not class probabilities. Cannot compute inverse entropy or max class probability without the full probability distribution. Will become implementable when FinBERT provides (pos, neg, neutral) probabilities.

### G-S2: Author credibility weighting (`wauthor`) missing -- OPEN

No author metadata is stored or used. The weight formula remains `w = wsrc * wrel * e^(-lambda*dt)` (three of five paper factors). AV's API does provide an `authors` field that is not currently captured. Phase 2+ scope.

### G-S6: Narrative sources incomplete -- OPEN (Phase 2+)

Only news articles from AV and Finnhub are ingested. The paper specifies six source types (news, press releases, earnings calls, blogs, forums, social media); four are missing. No changes since original audit.

### G-S7: Language detection absent -- OPEN (Phase 2+)

No language detection or filtering exists. Non-English articles (if any from AV/Finnhub) are scored and contribute to the sub-index. No changes since original audit.

### G-K1: Shrinkage factor -- ACCEPTED (intentional)

`compute_sub_index()` applies `min(1.0, n / 5)` shrinkage toward neutral when fewer than 5 signals exist. Not in the paper but is a documented, intentional engineering addition. No changes needed.

### G-K2: Relevance default 0.5 -- OPEN (minor)

Articles without `relevance_score` default to 0.5. Currently affects only AV articles where the per-ticker entry is missing from `ticker_sentiment` (rare). Finnhub articles have `relevance_score=None` but are excluded by the `provider_sentiment IS NOT NULL` gate. Will need attention in Phase 2 when FinBERT-scored Finnhub articles enter the scoring path.

---

## Regressions

None identified. All previously passing checks continue to pass:

- **Ingestion schedule:** `narrative_job` still runs every 30 minutes, 24/7 (scheduler.py:587-593). No change.
- **Schema completeness:** `raw_articles` schema unchanged. `event_cluster_id` and `finbert_score` columns remain present and Phase 2-ready.
- **Scoring formula:** `score_narrative_signals()` still maps `provider_sentiment` in [-1, +1] to [0, 100] via `50 + 50 * sentiment` (normalize.py:449). No change.
- **Sub-index aggregation:** `compute_sub_index()` still uses `raw = sum(score * weight) / sum(weight)` with shrinkage (subindices.py:70-74). No change.
- **Empty-window handling:** `_score_narrative()` still falls back to Redis cache via `_fallback_subindex()` when no articles exist (orchestrator.py:230). No change.
- **Output range:** [0, 100] with 50 = neutral. No change.
- **The Sprint 6 cluster_articles() addition does not break the no-cluster case:** When no articles have `event_cluster_id` set (e.g., fresh deployment, clustering not yet run), `COALESCE(event_cluster_id, id::text)` reduces to `id::text`, making each article its own group. DISTINCT ON with unique keys returns all rows. Scoring proceeds identically to the pre-Sprint-6 behavior.

---

## Conclusion

The narrative sub-index pipeline has meaningfully improved since the original audit. Four of 12 gaps are now resolved:

- **G-C3 (semantic dedup):** Fully wired. `cluster_articles()` runs in `narrative_job()` after article ingestion. `get_articles_since()` uses `DISTINCT ON` with cluster-aware dedup and selects the highest-relevance article per cluster. Telemetry tracks cluster formation metrics.
- **G-S3 (half-life):** Narrative half-life is now 12h (within the paper's 6-24h range), replacing the previous uniform 48h.
- **G-S4 (AV source weight):** Reduced from 1.0 to 0.7, correctly placing AV in the "News" tier.
- **G-S5 (relevance threshold):** Hard exclusion at relevance < 0.1 replaces the previous floor-only approach.

Two critical gaps (G-C1, G-C2) are reclassified as Accepted Phase 1 Simplifications. AV's provider_sentiment is a functioning external NLP model that produces format-compatible scores. Phase 1 implements the external-API stage; Phase 2 will bring NLP in-house via FinBERT, which will also unblock G-S1 (wconf) and fully resolve G-C1 (Finnhub scoring).

Five gaps remain open (G-S1, G-S2, G-S6, G-S7, G-K2), all either blocked by Phase 2 FinBERT or representing deliberate Phase 2+ scope boundaries. No regressions were identified.

**Phase 1 narrative pipeline compliance: adequate.** The pipeline correctly ingests, deduplicates (exact + semantic), filters (relevance threshold), weights (source weight + time decay at appropriate half-life), and aggregates (weighted average with shrinkage) AV-sourced news articles into a 0-100 sub-index. The two remaining weight factors (wconf, wauthor) and additional source types are Phase 2 scope.
