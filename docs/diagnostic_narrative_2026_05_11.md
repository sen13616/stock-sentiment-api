# Diagnostic Report: Narrative Pipeline — Current State Audit

**Date:** 2026-05-11
**Branch:** main (commit 8c40b33, post-Phase-1 methodology gate)
**Scope:** Read-only audit of the Public Narrative sub-index pipeline
**Author:** Claude Code diagnostic session
> **Subsequent revisions:**
> - **2026-05-11 (same day):** Gap **N-S1 (w_author missing)** was reconciled against the paper rather than via code change. Per the paper's Event-Level Weighting section, narrative `w_author` is folded into `w_src` for institutional and retail sources and is not a standalone term in the narrative weight formula. The deployed weight formula is therefore not missing this component. The Influencer-channel `w_author` (insider role) is still a real gap and is tracked in masterchecklist.md Phase 2.8.

---

## 1. Sources Wired

### Summary Table

| Paper Source Type | Wired? | Fetcher | API / Endpoint | Auth Tier | Cadence | Lands In | Scored? |
|---|---|---|---|---|---|---|---|
| News articles | **Yes** | `_fetch_av_news()` | AV `NEWS_SENTIMENT` | Free (5 req/min) | Every 30 min | `raw_articles` | **Yes** — AV's `ticker_sentiment_score` |
| News articles (fallback) | **Yes** | `_fetch_finnhub_news()` | Finnhub `/company-news` | Free | Every 30 min | `raw_articles` | **No** — `provider_sentiment=None` |
| Press releases | **Missing** | — | — | — | — | — | — |
| Earnings call transcripts | **Missing** | — | — | — | — | — | — |
| Blogs | **Missing** | — | — | — | — | — | — |
| Forums (Reddit/WSB) | **Missing** | — | — | — | — | — | — |
| Social media (Twitter/X) | **Missing** | — | — | — | — | — | — |

**Result: 2 of 6 paper-specified source types are wired. Of those 2, only 1 (AV) actually contributes to scoring.**

### Source-by-Source Details

#### Alpha Vantage NEWS_SENTIMENT — Implemented and Working

- **Fetcher:** `pipeline/sources/narrative.py:66-127` (`_fetch_av_news`)
- **Endpoint:** `https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={ticker}&limit=50`
- **Auth:** Free tier API key (`ALPHA_VANTAGE_KEY` from `.env`)
- **Cadence:** Every 30 min via `IntervalTrigger(minutes=30)` — `scheduler.py:588`
- **Lands in:** `raw_articles` table
- **Fields extracted from response:**
  - `url` → `source_url` + SHA-256 → `content_hash` (narrative.py:124, 49-51)
  - `time_published` → `published_at` (narrative.py:99, parsed at line 58-62)
  - `title` → `title` (capped 500 chars, line 117)
  - `summary` → `summary` (capped 2000 chars, line 118)
  - `ticker_sentiment[ticker].ticker_sentiment_score` → `provider_sentiment` (line 109)
  - `ticker_sentiment[ticker].relevance_score` → `relevance_score` (line 110)
- **Sentiment scoring:** AV's pre-computed `ticker_sentiment_score` field is used directly. This is an external NLP model score in [-1, +1]. It is NOT a local model, NOT a regex, NOT hard-coded. It maps to the paper's `S_e = P(pos) - P(neg)` format.

#### Finnhub /company-news — Implemented but Unscored

- **Fetcher:** `pipeline/sources/narrative.py:134-191` (`_fetch_finnhub_news`)
- **Endpoint:** `https://finnhub.io/api/v1/company-news?symbol={ticker}&from={3d_ago}&to={today}`
- **Auth:** Free tier API key (`FINNHUB_KEY` from `.env`)
- **Cadence:** Same 30-min job as AV (both fetched in `narrative_job` Phase 1)
- **Lands in:** `raw_articles` table
- **Critically:** `provider_sentiment` is set to `None` (narrative.py:186), `relevance_score` is set to `None` (narrative.py:187)
- **Scoring impact:** `get_articles_since()` query filters `WHERE provider_sentiment IS NOT NULL` (raw_articles.py:128), so Finnhub articles are **completely excluded from scoring** in Phase 1. They exist in the DB as data storage for future Phase 2 FinBERT scoring.

#### NewsAPI — NOT Wired

- **Status:** Not wired into the scheduler or any fetcher function
- **Evidence:**
  - `normalize.py:47` has a comment: `# Removed Stage 1 — "newsapi" excluded from paper's implemented methodology.`
  - No `_fetch_newsapi_news()` or equivalent function exists in `pipeline/sources/narrative.py`
  - The scheduler's `narrative_job()` (scheduler.py:360-434) calls only `fetch_narrative_signals` which internally calls `_fetch_av_news` + `_fetch_finnhub_news`
  - `README.md` still references NewsAPI in multiple places (lines 116, 265, 496, 918, 970, 1052, 1114) — **the README is stale on this point**
- **Was it ever wired?** The comment in normalize.py suggests it was previously included but removed during Stage 1 cleanup. No code path calls it.

#### Reddit / WSB / Any Social Media — Missing Entirely

- **Status:** No code, no stubs, no fetcher, no scheduler job
- **Evidence:**
  - `README.md:117` mentions "ApeWisdom" as "opportunistic" and `README.md:266` mentions "Reddit / social: 0.40" weight, but these are aspirational README entries only
  - No file `pipeline/sources/social.py` or equivalent exists
  - No import or function referencing reddit, wsb, apewisdom, twitter, or stocktwits anywhere in pipeline code
  - Grep across entire codebase: references only in README.md and audit docs (docs-only, no code)

#### Earnings Call Transcripts — Missing Entirely

- **Status:** No code, no stubs
- **Evidence:**
  - `docs/sprint_plan.md:590` lists "Earnings call transcripts (Phase 2.3)" as "Blocked on data source decision"
  - No transcript-fetching code exists anywhere in the codebase
  - SEC EDGAR integration exists for Form 4 (insider trades) but NOT for 8-K filings or transcript text

#### Press Releases, Blogs, Forums — Missing Entirely

- No code, no stubs, no schema provisions for any of these source types

### Production DB Sample Counts

**Cannot query production DB from this session.** The diagnostic has no DB credentials or connection. Production health numbers in Section 5 are marked as "requires manual verification."

---

## 2. Per-Article Scoring Path

### AV Articles: Full Scoring Path

```
AV API response
    ↓
narrative.py:109     provider_sentiment = float(ts["ticker_sentiment_score"])  # [-1, +1]
narrative.py:110     relevance_score = float(ts["relevance_score"])            # [0, 1]
narrative.py:124     content_hash = SHA-256(url)
    ↓
raw_articles.py:46-52   INSERT ... ON CONFLICT (ticker, content_hash) DO NOTHING
    ↓  [stored in raw_articles table]
    ↓  [scoring_tick_job fires every 30 min]
    ↓
orchestrator.py:222  articles = await get_articles_since(ticker, since)
    ↓
raw_articles.py:119-138  SELECT with DISTINCT ON (COALESCE(event_cluster_id, id::text))
                         WHERE provider_sentiment IS NOT NULL
                         ORDER BY relevance_score DESC (per cluster)
    ↓
orchestrator.py:224  sigs = score_narrative_signals(ticker, articles, now)
    ↓
normalize.py:437     sentiment = art.get("provider_sentiment")   # raw [-1, +1]
normalize.py:441     if math.isnan(sent_f) or math.isinf(sent_f): continue   # NaN guard
normalize.py:443     relevance = float(art.get("relevance_score") or 0.5)     # default 0.5
normalize.py:444-445 if relevance < 0.1: continue                              # hard threshold
normalize.py:449     score = max(0.0, min(100.0, 50.0 + 50.0 * sent_f))       # → [0, 100]
normalize.py:450     half_life = _get_half_life("narrative", source)            # 12h for narrative
normalize.py:451     w_time = _time_weight(published, now, half_life)           # exp(-λt)
normalize.py:452     weight = _source_weight(source) * w_time * relevance       # combined weight
    ↓
    {signal_type: "provider_sentiment", score: [0,100], weight: combined, source: "alpha_vantage"}
    ↓
orchestrator.py:225  si = compute_sub_index(sigs)   # generic weighted average
```

### Finnhub Articles: Blocked at Query Level

```
Finnhub API response
    ↓
narrative.py:186     provider_sentiment = None       # ← NO sentiment from Finnhub
narrative.py:187     relevance_score = None           # ← NO relevance from Finnhub
    ↓
raw_articles.py:46-52   INSERT ... ON CONFLICT DO NOTHING
    ↓  [stored in raw_articles table with NULLs]
    ↓  [scoring_tick_job fires]
    ↓
raw_articles.py:128  WHERE provider_sentiment IS NOT NULL   ← EXCLUDED HERE
    ↓
    (article never reaches scoring path)
```

### What Populates Each Key Field

| Field | AV Source | Finnhub Source | File:Line |
|---|---|---|---|
| `provider_sentiment` | AV `ticker_sentiment_score` (float, [-1,+1]) | `None` (not provided) | narrative.py:109 / 186 |
| `relevance_score` | AV `relevance_score` per ticker (float, [0,1]) | `None` (not provided) | narrative.py:110 / 187 |
| `content_hash` | SHA-256 of article URL (64 hex chars) | SHA-256 of article URL | narrative.py:124, 49-51 / 188 |
| `finbert_score` | Not set (reserved for Phase 2) | Not set | — |
| `event_cluster_id` | Written by `cluster_articles()` post-fetch | Written by `cluster_articles()` post-fetch | dedup.py:148 |

### Relevance Score Interpretation

The `relevance_score` from AV is the API's ticker-relevance estimate — **not** the paper's formal `w_rel = P(relevant to target asset)` from a dedicated relevance classifier. AV's field serves as a proxy. When absent (Finnhub, or AV articles where the ticker isn't in the sentiment list), it defaults to `0.5` (normalize.py:443). This is used directly as a multiplicative weight factor in the combined weight formula.

### Dedup Status

- **Stage 1 (hash dedup):** Implemented and firing. `ON CONFLICT (ticker, content_hash) DO NOTHING` at raw_articles.py:52. Hash computed from URL.
- **Stage 2 (semantic dedup):** Implemented and firing (Sprint 6, G-C6). `cluster_articles()` runs per-ticker in Phase 2 of `narrative_job()` (scheduler.py:395-403). Uses `all-MiniLM-L6-v2` embeddings, Union-Find clustering within 4-hour window, cosine > 0.85 threshold. Scoring query selects highest-relevance article per cluster (raw_articles.py:119-138).

### Audit Re-Run Verification (G-C4 and G-C5)

Per `docs/audit_B_narrative_postphase1.md`:

- **G-C4 (Finnhub articles unscored):** Reclassified as **"Accepted Phase 1 Simplification."** The audit confirms Finnhub articles are stored with `provider_sentiment=None` and excluded from scoring. This remains unchanged. Phase 2 FinBERT will resolve it via `COALESCE(finbert_score, provider_sentiment)`.

- **G-C5 (No local NLP model):** Reclassified as **"Accepted Phase 1 Simplification."** All scoring uses AV's external `ticker_sentiment_score`. The `finbert_score` column exists in the schema (all NULLs). `pipeline/nlp/finbert.py` is planned but not implemented. Phase 2 deliverable.

**Both statements still hold** as of commit 8c40b33. No code changes have occurred that affect these since the post-Phase-1 audit.

---

## 3. Sub-Index Aggregation Math

### Implemented Formula

**Location:** `pipeline/scoring/subindices.py:46-81` (`compute_sub_index`)

```python
# Line 70: weighted average
raw = sum(s["score"] * s["weight"] for s in valid) / total_w

# Line 73-74: volume shrinkage toward neutral
shrinkage = min(1.0, n / 5)
value = 50.0 + shrinkage * (raw - 50.0)
```

**This is:**
```
raw   = Σ(w_i × score_i) / Σ(w_i)
value = 50 + min(1, n/5) × (raw − 50)
```

### Paper's Formula (page 12)

```
S_t^(c) = Σ(w_i × S_i) / Σ(w_i)
```

Where `w_i = w_src × w_rel × w_conf × w_author × exp(-λΔt)`.

### Comparison

The **structural form** matches — both are weighted averages with normalization by total weight. The shrinkage toward neutral (line 73-74) is an engineering addition not in the paper (documented as intentional deviation G-K1 in the audit).

The **critical difference** is in what constitutes `w_i`. The paper specifies five weight factors; the implementation has three:

| Weight Factor | Paper Formula | Implementation | Status | File:Line |
|---|---|---|---|---|
| `w_src` (source reliability) | 0.6-0.8 for news, 0.2-0.4 for social | `_SOURCE_WEIGHTS["alpha_vantage"] = 0.7` | **Implemented** | normalize.py:42 |
| `w_rel` (relevance) | `P(relevant to target asset)` from classifier | AV's `relevance_score` used as proxy | **Partially implemented** — uses API value, not own classifier | normalize.py:443, 452 |
| `w_conf` (model confidence) | `max(P(k))` or `1 - H(P)/log(K)` | **Missing** | Blocked by FinBERT (no class probabilities available from AV) | — |
| `w_author` (author credibility) | Rule-based heuristic per author | **Missing** | No author metadata stored or used | — |
| `exp(-λΔt)` (time decay) | Per-source half-life decay | `_time_weight()` with 12h half-life | **Implemented** | normalize.py:81-87, 62, 450-451 |

### Time Decay Half-Life

- **Paper spec:** News/media: 6-24 hours (page 11)
- **Deployed config:** 12 hours — `_LAYER_HALF_LIFE_H["narrative"] = 12.0` (normalize.py:62)
- **Wired at:** normalize.py:450 calls `_get_half_life("narrative", source)`, which returns 12.0 for all narrative sources (no narrative-specific source overrides in `_HALF_LIFE_OVERRIDE`)
- **Assessment:** 12h is within the paper's 6-24h range. Appropriate for Phase 1.

Note: The paper specifies social media half-life at 1-3 hours. Since no social media sources are wired, this isn't implemented. If social media is added in Phase 2, a source-level override will be needed (e.g., `("narrative", "reddit"): 2.0` in `_HALF_LIFE_OVERRIDE`).

### Score Transformation

- **Paper:** `S_e = P(pos) - P(neg)` produces [-1, +1]
- **Implementation:** AV returns `ticker_sentiment_score` in [-1, +1] (format-compatible), then mapped to [0, 100] via `score = 50.0 + 50.0 * sentiment` (normalize.py:449)
- **Assessment:** The linear mapping preserves the ordering and neutral point. Score 50 = sentiment 0.0 = neutral. This is correct.

### Parametric Fallback / Cold-Start for Narrative

- **No z-score normalization for narrative signals.** The `_ZSCORE_CONFIG` dictionary (normalize.py:186-200) contains entries for market, influencer, and macro signal types, but **no entry for `provider_sentiment`**.
- Narrative scoring is deterministic: `score = 50 + 50 * sentiment_value`. No RollingZScorer is involved.
- **Cold-start fallback:** When no articles exist in the 3-day lookback window, `_fallback_subindex("narrative", last_state, now)` (orchestrator.py:230) recovers the last cached narrative sub-index from Redis, subject to a 6-hour staleness window (`_LAYER_LOOKBACK["narrative"] = timedelta(hours=6)`, orchestrator.py:85).
- If Redis cache is also stale or absent: narrative layer is `None` (missing), composite weight is redistributed across remaining layers, and a 15-point confidence penalty applies.

---

## 4. Direct vs. Indirect Narrative — Scaffolding Status

### Does any concept of "direct vs. indirect" exist? **No.**

Systematic check:

| Question | Answer | Evidence |
|---|---|---|
| Field distinguishing "article about ticker X" vs "article mentions ticker X"? | **No** | `raw_articles` schema (migrations.sql:27-41) has no such field. The only ticker-related fields are `ticker` (VARCHAR) and `relevance_score` (FLOAT). |
| How is ticker assignment done? | **One row per (ticker, article) pair** | The fetcher queries AV with `tickers={ticker}` (narrative.py:76) — the API returns articles relevant to that specific ticker. Each article is stored once per ticker it was fetched for. |
| Does an article get multiple tickers? | **Only if fetched separately for each** | AV's `ticker_sentiment` list contains per-ticker scores, but the fetcher only extracts the score for the requesting ticker (narrative.py:107). If AV returns the same article for AAPL and MSFT in separate fetcher calls, it would be stored as two rows (different `ticker`, same `source_url`, same `content_hash` — but the unique index is on `(ticker, content_hash)` so both rows can coexist). |
| Multi-ticker articles downweighted? | **No mechanism** | There is no cross-ticker awareness. The `relevance_score` from AV may be lower for tangentially mentioned tickers, which provides *implicit* downweighting, but there is no explicit check for "article mentions N tickers, downweight by 1/N" or similar. |
| Sector/macro narrative pipeline? | **No** | Articles are fetched per-ticker only. There is no sector-level or market-wide narrative aggregation. The macro sub-index uses VIX + sector ETFs (quantitative signals), not textual sources. |

### What Would "Direct vs. Indirect" Require?

To implement the Phase 2 concept:

1. **Schema change:** A field like `narrative_type ENUM('direct', 'indirect', 'sector')` on `raw_articles`, or a `ticker_mention_count INT` field
2. **Cross-ticker awareness:** After fetching an article, check how many S&P 500 tickers appear in AV's `ticker_sentiment` list
3. **Weight modifier:** Downweight articles that mention many tickers (indirect/sector signal) relative to articles focused on a single ticker (direct signal)
4. **Sector aggregation:** Optionally aggregate "indirect" articles at the sector level and feed them into the macro or a new sector-narrative sub-index

None of this scaffolding currently exists. It would be new Phase 2 work.

---

## 5. Production Health for Narrative

### Cannot Query Production DB

This diagnostic session does not have database credentials or a connection to the production PostgreSQL instance. The following queries would be needed to complete this section:

```sql
-- Last 24h narrative scoring activity
SELECT COUNT(DISTINCT ticker) as tickers_scored,
       COUNT(*) as total_rows,
       MIN(narrative_index) as min_narrative,
       AVG(narrative_index) as avg_narrative,
       MAX(narrative_index) as max_narrative,
       COUNT(*) FILTER (WHERE narrative_index IS NULL) as null_count
FROM sentiment_history
WHERE created_at >= NOW() - INTERVAL '24 hours';

-- Distribution of narrative scores
SELECT
    width_bucket(narrative_index, 0, 100, 10) as bucket,
    COUNT(*) as count
FROM sentiment_history
WHERE created_at >= NOW() - INTERVAL '24 hours'
  AND narrative_index IS NOT NULL
GROUP BY bucket ORDER BY bucket;

-- Tickers with no narrative coverage (last 7d)
SELECT t.ticker
FROM ticker_universe t
WHERE t.tier1_supported = true
  AND NOT EXISTS (
    SELECT 1 FROM raw_articles ra
    WHERE ra.ticker = t.ticker
      AND ra.published_at >= NOW() - INTERVAL '7 days'
      AND ra.provider_sentiment IS NOT NULL
  )
ORDER BY t.ticker
LIMIT 20;

-- Article scoring status (last 7d)
SELECT
    COUNT(*) as total_articles,
    COUNT(*) FILTER (WHERE provider_sentiment IS NULL) as null_sentiment,
    COUNT(*) FILTER (WHERE provider_sentiment = 0) as zero_sentiment,
    COUNT(*) FILTER (WHERE provider_sentiment IS NOT NULL AND provider_sentiment != 0) as scored,
    COUNT(*) FILTER (WHERE relevance_score IS NULL) as null_relevance,
    COUNT(*) FILTER (WHERE relevance_score < 0.1) as below_threshold
FROM raw_articles
WHERE published_at >= NOW() - INTERVAL '7 days';

-- By source breakdown (last 7d)
SELECT source, COUNT(*) as article_count,
       COUNT(*) FILTER (WHERE provider_sentiment IS NOT NULL) as with_sentiment
FROM raw_articles
WHERE published_at >= NOW() - INTERVAL '7 days'
GROUP BY source;
```

**Recommendation:** Run these queries manually against the production database and append results to this report. The Finnhub vs. AV article count ratio will reveal whether Finnhub articles represent a material fraction of unused narrative data.

### Proxy Indicator from Audit Logs

From the Sprint 6 verification (masterchecklist.md, G-C6 item): "2,151 articles clustered, 886 clusters, 496 cross-source clusters (56%), 13,446 total articles in 48h" — this was measured on 2026-05-10, indicating substantial article volume and healthy dedup activity. However, these numbers include Finnhub articles (which are stored but unscored).

---

## 6. Gap List vs. Paper

### Severity Key

- **C (Critical):** The gap means a core paper formula component is absent or fundamentally wrong. The paper's methodology section cannot be written accurately without addressing this.
- **S (Significant):** The gap is a meaningful deviation that degrades the quality of the narrative sub-index but doesn't invalidate it. Should be addressed before validation.
- **K (Cosmetic/Known):** Engineering choice, minor deviation, or already-documented trade-off. Acceptable to document in the paper's limitations section.

### Gap Table

| ID | Gap Description | Severity | Status | File:Line |
|---|---|---|---|---|
| **N-C1** | **Only 1 of 6 source types contributes to scoring** (AV news only; Finnhub stored but unscored; 4 source types completely missing) | C | Phase 2 | narrative.py:186 (Finnhub NULL), scheduler.py:360 (only AV+Finnhub in job) |
| **N-C2** | **No local NLP model** — all sentiment from AV's external API; no control over model quality, no class probabilities | C | Phase 2 (FinBERT) | normalize.py:437-449 (uses provider_sentiment only) |
| **N-C3** | **`w_conf` (model confidence weight) missing** — paper specifies `max(P(k))` or inverse entropy; impossible without class probabilities | C | Blocked by N-C2 | normalize.py:452 (weight has no conf term) |
| **N-S1** | **`w_author` (author credibility weight) missing** — no author metadata captured from either AV or Finnhub | S | Phase 2 | narrative.py (no `author` field extracted); migrations.sql:27-41 (no `author` column) |
| **N-S2** | **No relevance classifier** — paper specifies `w_rel = P(relevant to target asset)` from a trained model; implementation uses AV's API field as proxy | S | Acceptable Phase 1 proxy; improve in Phase 2 | normalize.py:443 |
| **N-S3** | **No language detection** — non-English articles (if any) scored and contribute to sub-index | S | Phase 2 (G-S16) | No langdetect/fasttext in pipeline |
| **N-S4** | **Relevance default of 0.5 for articles without relevance score** — when AV doesn't include a ticker in `ticker_sentiment`, `relevance_score` defaults to 0.5 instead of being excluded | S | normalize.py:443 |
| **N-S5** | **README.md stale** — still references NewsAPI (lines 116, 265, 496, 918, 970, 1052, 1114) and ApeWisdom (line 117, 497, 918, 1115) as if they are wired; they are not | S | README.md (multiple lines) |
| **N-K1** | **Shrinkage factor `min(1, n/5)`** not in paper — engineering addition that pulls narrative toward neutral when few articles exist | K | Intentional (documented G-K5) | subindices.py:73 |
| **N-K2** | **No z-score normalization for narrative** — market and influencer signals use adaptive z-score; narrative uses deterministic `50 + 50 * sentiment` | K | By design — sentiment already in [-1,+1] from AV; z-score would normalize opinions, not raw measurements | normalize.py:449 |
| **N-K3** | **No aspect-based sentiment (ABSA)** — paper page 6 mentions "aspect levels"; no schema or code for it | K | Phase 2 item | — |
| **N-K4** | **No direct/indirect narrative distinction** — no schema, no logic, no concept of ticker-specific vs. sector-wide articles | K | Phase 2 concept | migrations.sql:27-41 |
| **N-K5** | **Social media half-life not configured** — paper says 1-3h for social; only the 12h news half-life exists. Will need a source-level override if social sources are added | K | Irrelevant until social sources wired | normalize.py:60-70 |

### Cross-Reference with Existing Tracker

| This Report | Existing Tracker ID | Audit B ID | Notes |
|---|---|---|---|
| N-C1 | G-C4 + G-S15 | G-C1 + G-S6 | Combines Finnhub unscored + sources incomplete |
| N-C2 | G-C5 | G-C2 | Same: no FinBERT |
| N-C3 | G-S11 | G-S1 | Same: no w_conf |
| N-S1 | G-S12 | G-S2 | Same: no w_author |
| N-S3 | G-S16 | G-S7 | Same: no language detection |
| N-S4 | — | G-K2 | Relevance default 0.5 (not in master tracker by this exact framing) |
| N-K1 | G-K5 | G-K1 | Shrinkage factor |

---

## 7. Open Questions

These are items this diagnostic could not resolve from code alone:

1. **What fraction of Finnhub articles overlap with AV articles for the same ticker?** The semantic dedup clusters cross-source articles (56% of clusters were cross-source per Sprint 6 metrics), but we don't know how many Finnhub articles represent *unique events* that AV missed entirely. This determines the information loss from not scoring Finnhub articles. **Requires: production DB query.**

2. **What is the actual article volume per ticker?** Some S&P 500 tickers (large-cap tech) likely get dozens of articles per day from AV; smaller tickers may get zero. The narrative sub-index quality is highly uneven across the universe. **Requires: production DB query.**

3. **How many tickers regularly receive zero narrative articles?** These tickers' narrative sub-index is always `None` (missing layer), which means their composite score is computed from only 3 sub-indices with redistributed weights. This is a systematic bias. **Requires: production DB query.**

4. **Is AV's `ticker_sentiment_score` calibrated?** The paper assumes `S_e = P(pos) - P(neg)` from a softmax distribution. AV's score is a black box — we don't know if it follows this formula or uses a different calibration. The linear mapping `50 + 50 * score` treats it as if the score were in [-1, +1] with uniform semantics, but this is an assumption.

5. **Should the paper's "Public Narrative" section describe the Phase 1 implementation honestly (AV-only, external NLP) or the target Phase 2 architecture (FinBERT + multi-source)?** This is a writing decision, not a code decision. Recommendation: describe the architecture generically with the paper's formulas, note the Phase 1 simplifications in a limitations subsection, and indicate Phase 2 as future work.

6. **Does the paper need to specify a minimum article count for the narrative sub-index to be considered valid?** Currently, even 1 article can produce a narrative sub-index (though shrinkage pulls it toward neutral). The paper doesn't specify a minimum, but this has quality implications.

7. **When social media sources are added, should they flow through the same `raw_articles` table or a separate `raw_social` table?** Social media signals (mentions, sentiment, volume) have fundamentally different structure from articles (no body text, no URL, no title in the same sense). The current schema may not be a good fit.

---

## One-Paragraph Summary

**The narrative pipeline's most important finding is that it is operating on a single effective data source (Alpha Vantage NEWS_SENTIMENT) while the paper specifies six source types, and 2 of the 5 event-level weight factors (`w_conf`, `w_author`) are completely unimplemented.** Finnhub articles are ingested and stored (and even semantically deduplicated), but they contribute zero information to scoring because they lack `provider_sentiment` and no local NLP model exists to score them. The structural formula for sub-index aggregation (weighted average with normalization) matches the paper, and the time decay half-life (12h) is within the paper's specified range. However, the weight composition `w = w_src * w_time * w_rel` is missing 40% of the paper's specified factors. This means the "Public Narrative Signals" section of the paper must either (a) honestly describe a Phase 1 simplification with external NLP and partial weighting, or (b) wait for Phase 2 FinBERT + additional sources before being written as the paper's methodology specifies.
