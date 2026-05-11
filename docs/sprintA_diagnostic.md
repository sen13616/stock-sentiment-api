# Sprint A Diagnostic — FinBERT + w_conf + Source Weight Reconciliation

**Branch:** `feature/sprint-A-finbert` (off `main` at `8c40b33`)
**Date:** 2026-05-11
**Scope:** Phase 2 items 2.1 (FinBERT), 2.2 (w_conf), source weight updates
**Status:** Diagnostic complete — awaiting approval before implementation

---

## 1. Current Narrative Scoring Path (Post-Sprint-D)

### Data flow

```
narrative_job Phase 1: Fetch
  AV NEWS_SENTIMENT → insert_article(provider_sentiment=float, relevance_score=float)
  Finnhub /company-news → insert_article(provider_sentiment=None, relevance_score=None)

narrative_job Phase 2: Cluster
  cluster_articles() → writes event_cluster_id to raw_articles

scoring_tick_job:
  get_articles_since(ticker, since)
    → WHERE provider_sentiment IS NOT NULL   ← Finnhub excluded here
    → DISTINCT ON (COALESCE(event_cluster_id, id::text))
  score_narrative_signals(ticker, articles, now)
    → sentiment = art["provider_sentiment"]           ← AV's external NLP
    → relevance filter: None → skip, < 0.6 → skip    ← Sprint D
    → score = 50 + 50 * sent_f                        ← linear [-1,+1] → [0,100]
    → weight = _source_weight(source) * w_time * relevance   ← NO w_conf
```

### Key files and line references

| File | Function/Location | Role |
|------|-------------------|------|
| `pipeline/sources/narrative.py:66-127` | `_fetch_av_news()` | Fetches AV articles, extracts `provider_sentiment` and `relevance_score` |
| `pipeline/sources/narrative.py:134-191` | `_fetch_finnhub_news()` | Fetches Finnhub articles, sets `provider_sentiment=None` |
| `db/queries/raw_articles.py:94-138` | `get_articles_since()` | WHERE `provider_sentiment IS NOT NULL` — Finnhub excluded |
| `pipeline/features/normalize.py:422-466` | `score_narrative_signals()` | Uses `provider_sentiment`, applies relevance filter, computes weight without `w_conf` |
| `pipeline/features/normalize.py:41-50` | `_SOURCE_WEIGHTS` | AV=0.70, Finnhub=0.80 (pre-paper values) |
| `pipeline/features/normalize.py:455` | Weight formula | `_source_weight(source) * w_time * relevance` |
| `pipeline/orchestrator.py:216-231` | `_score_narrative()` | Calls `get_articles_since` then `score_narrative_signals` |

### Production article volume (7-day window, queried 2026-05-11)

| Source | Articles | Has provider_sentiment | Has finbert_score | Has relevance_score | Tickers |
|--------|----------|----------------------|-------------------|--------------------|---------|
| alpha_vantage | 25,992 | 25,992 (100%) | 0 | 25,992 (100%) | 487 |
| finnhub | 18,071 | 0 (0%) | 0 | 0 (0%) | 478 |
| **Total** | **44,038** | **25,992 (59%)** | **0** | **25,992 (59%)** | — |

**Critical finding:** Finnhub articles are 41% of total article volume (18,071/44,038) but contribute zero to scoring. Post-Sprint-A, all 44,038 articles per 7-day window will be FinBERT-scored, increasing the narrative signal pool by ~69% (from 25,992 to 44,038, minus articles failing relevance/language filters).

### Avg new articles per 30-min window: ~131

This is the FinBERT Phase 3 batch size per `narrative_job` run. At ~100ms/article on CPU: ~13 seconds. Well within the 30-minute job budget.

---

## 2. Schema Additions Needed

### Current `raw_articles` columns

```
id, ticker, title, summary, source, source_url, published_at,
provider_sentiment, relevance_score, content_hash, event_cluster_id,
finbert_score, created_at
```

### Column status

| Column | Exists | Populated | Action |
|--------|--------|-----------|--------|
| `finbert_score` | Yes (`migrations.sql:39`) | All NULLs (0/44k) | Will be populated by FinBERT Phase 3 |
| `finbert_pos` | **No** | — | **Add in migration 007** |
| `finbert_neg` | **No** | — | **Add in migration 007** |
| `finbert_neu` | **No** | — | **Add in migration 007** |
| `language` | **No** | — | **Add in migration 007** |

### Migration 007 design

```sql
ALTER TABLE raw_articles
    ADD COLUMN IF NOT EXISTS finbert_pos DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS finbert_neg DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS finbert_neu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS language VARCHAR(10);
```

All columns nullable. No indexes needed — these are write-once read-at-scoring-time fields, accessed via the existing `idx_raw_articles_ticker_ts` index.

Existing migration numbering: 005 (company_name), 006 (ema columns). Next: 007.

### `get_articles_since()` query changes

**Current** (`raw_articles.py:119-137`):
- SELECT: `published_at, provider_sentiment, relevance_score, source`
- WHERE: `provider_sentiment IS NOT NULL`

**Post-Sprint-A:**
- SELECT: `published_at, finbert_score, relevance_score, source, finbert_pos, finbert_neg, finbert_neu`
- WHERE: `finbert_score IS NOT NULL` (replaces `provider_sentiment IS NOT NULL`)

This is the gate change that unlocks Finnhub articles for scoring. Articles without FinBERT scores (unscored backlog, non-English) are excluded.

### Relevance for Finnhub articles

Finnhub articles currently have `relevance_score = NULL`. Post-Sprint-D, `relevance_raw is None → continue` at `normalize.py:443-445` would exclude them even after FinBERT scores them.

**Paper resolution:** Per paper Stage 2 (lines 1120-1122): "Finnhub company-news endpoint is inherently ticker-keyed... these enter at w_rel = 1.0 by construction."

**Implementation:** Set `relevance_score = 1.0` for Finnhub articles at ingestion time (`narrative.py:187`). This is a one-line change: `"relevance_score": 1.0` instead of `"relevance_score": None`.

Existing Finnhub articles in the DB with `relevance_score = NULL` will still be excluded by the None check in `normalize.py:443-445`. They need a backfill UPDATE or the FinBERT backfill script can set relevance alongside finbert_score.

---

## 3. FinBERT Inference Infrastructure

### Module design: `pipeline/nlp/finbert.py`

Following the lazy-loading pattern from `pipeline/nlp/dedup.py:49-56`:

```python
_model = None
_tokenizer = None

def _get_model():
    global _model, _tokenizer
    if _model is None:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        _model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        _model.eval()
    return _model, _tokenizer
```

### Score computation (paper formula)

Paper Stage 3: `S_i = P_i(positive) - P_i(negative)`

```python
def score_article(text: str) -> dict:
    """Returns {finbert_score, finbert_pos, finbert_neg, finbert_neu}."""
    model, tokenizer = _get_model()
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        logits = model(**inputs).logits
    probs = torch.softmax(logits, dim=1).squeeze()
    # ProsusAI/finbert label order: positive=0, negative=1, neutral=2
    pos, neg, neu = probs[0].item(), probs[1].item(), probs[2].item()
    return {
        "finbert_score": pos - neg,
        "finbert_pos": pos,
        "finbert_neg": neg,
        "finbert_neu": neu,
    }
```

### Batch inference

For 131 articles per 30-min window, batch inference with padding is efficient:

```python
def score_batch(texts: list[str]) -> list[dict]:
    model, tokenizer = _get_model()
    inputs = tokenizer(texts, return_tensors="pt", truncation=True,
                       max_length=512, padding=True)
    with torch.no_grad():
        logits = model(**inputs).logits
    probs = torch.softmax(logits, dim=1)
    results = []
    for i in range(len(texts)):
        pos, neg, neu = probs[i][0].item(), probs[i][1].item(), probs[i][2].item()
        results.append({
            "finbert_score": pos - neg,
            "finbert_pos": pos,
            "finbert_neg": neg,
            "finbert_neu": neu,
        })
    return results
```

### Input text strategy

FinBERT's max sequence length is 512 tokens. Articles have title (max 500 chars) and summary (max 2000 chars). Production article text length distribution (7-day window):

| Length bucket (title+summary) | Count | % |
|-------------------------------|-------|---|
| < 100 chars | 647 | 1.5% |
| 100-500 chars | 23,582 | 53.6% |
| 500-1000 chars | 19,790 | 44.9% |
| 1000-2000 chars | 13 | <0.1% |
| 2000+ chars | 6 | <0.1% |

**Strategy:** Concatenate `title + " " + summary`, pass to tokenizer with `truncation=True, max_length=512`. The tokenizer handles the truncation. 98.5% of articles are < 1000 chars, which is well within 512 tokens (~380 words).

### New dependencies

| Package | Size | Required by |
|---------|------|-------------|
| `transformers` | ~700 MB installed | FinBERT model loading and tokenization |
| `torch` | ~2 GB installed (CPU-only) | FinBERT inference backend |
| `langdetect` | ~50 KB | Language detection (section 4) |

**Railway memory impact:** ProsusAI/finbert model weights are ~440 MB in memory. Railway Hobby plan provides 8 GB RAM per service. Current resident memory (estimated): FastAPI + asyncpg + Redis + sentence-transformers (~80 MB) + APScheduler ≈ 500 MB. Post-FinBERT: ~1 GB. Headroom is ample.

**Installation:** Add to `requirements.txt`:
```
transformers>=4.40.0
torch>=2.0.0
langdetect>=1.0.9
```

Note: `sentence-transformers==3.0.0` already pulls in `torch` as a transitive dependency. Adding `torch` explicitly ensures version control.

### Model download

ProsusAI/finbert (~440 MB) downloads on first use. On Railway, this happens during the first `narrative_job` run after deploy. The download is cached in the HuggingFace cache directory (`~/.cache/huggingface/`). Alternative: include model in Docker layer or use `HF_HOME` env var.

---

## 4. Language Detection

### Purpose

Paper assumes FinBERT-compatible text (English financial text). Non-English articles should be excluded from FinBERT scoring to avoid garbage scores.

### Implementation: `langdetect` at ingestion time

Add language detection in `pipeline/sources/narrative.py` before `insert_article()`:

```python
from langdetect import detect, LangDetectException

def _detect_language(text: str) -> str | None:
    try:
        return detect(text)
    except LangDetectException:
        return None
```

Call on `title + " " + (summary or "")`. Store result in `raw_articles.language`. Filter: only articles with `language = 'en'` pass to FinBERT.

### Non-English article volume estimate

The regex-based non-ASCII check failed on the production DB due to encoding issues. However, both AV NEWS_SENTIMENT and Finnhub /company-news return primarily English-language financial news. Non-English articles are expected to be < 5% of volume based on source characteristics.

### Filter placement

Two options:
1. **At ingestion** (in `narrative.py`): Detect language, store in DB, skip FinBERT for non-English. Pro: no wasted FinBERT inference. Con: langdetect on every article at ingestion.
2. **At FinBERT Phase 3** (in `scheduler.py`): Detect language for unscored articles, store, skip non-English. Pro: language detection batched with FinBERT. Con: extra DB read/write.

**Recommendation:** Option 1 — detect at ingestion. `langdetect` is fast (~1ms/article) and running it at ingestion avoids accumulating unscored non-English articles that would need to be repeatedly fetched and skipped.

---

## 5. Scheduler Integration

### narrative_job: Add Phase 3 (FinBERT scoring)

Current `narrative_job()` at `scheduler.py:360-434` has two phases:
1. **Phase 1: Fetch** — ingests articles from AV + Finnhub
2. **Phase 2: Cluster** — semantic dedup on unclustered articles

Add **Phase 3: FinBERT score** after clustering:

```python
# ── Phase 3: FinBERT scoring (Sprint A) ────────────────────────────
from db.queries.raw_articles import get_unscored_articles, update_finbert_scores
from pipeline.nlp.finbert import score_batch

unscored = await get_unscored_articles(since_hours=48.0, language='en')
if unscored:
    texts = [(a["title"] or "") + " " + (a["summary"] or "") for a in unscored]
    scores = score_batch(texts)
    await update_finbert_scores(
        [(a["id"], s["finbert_score"], s["finbert_pos"], s["finbert_neg"], s["finbert_neu"])
         for a, s in zip(unscored, scores)]
    )
```

### New DB functions needed

| Function | Purpose |
|----------|---------|
| `get_unscored_articles(since_hours, language)` | Fetch articles where `finbert_score IS NULL AND language = 'en'` |
| `update_finbert_scores(rows)` | Batch UPDATE `finbert_score`, `finbert_pos`, `finbert_neg`, `finbert_neu` by article ID |

### Timing within narrative_job

Phase 3 runs AFTER Phase 2 (clustering). This ensures:
- Articles are already inserted (Phase 1 complete)
- Cluster IDs are assigned (Phase 2 complete)
- FinBERT scores are written before the next `scoring_tick_job` reads them

The scoring tick runs every 30 min, same as narrative_job. As long as narrative_job completes before the next scoring tick, FinBERT scores are available. With ~131 articles × ~100ms = ~13 seconds for FinBERT Phase 3, this is well within budget.

---

## 6. Source Weight Update Sequencing

### Paper-specified weights vs current code

| Source | Paper weight | Current code (`normalize.py:41-50`) | Change |
|--------|-------------|-------------------------------------|--------|
| alpha_vantage | 0.75 | 0.70 | +0.05 |
| finnhub | 0.65 | 0.80 | -0.15 |
| sec_edgar | 1.00 | 1.00 | No change |
| polygon | — | 0.90 | No change |
| computed | — | 0.85 | No change |
| yfinance | — | 0.90 | No change |
| finra_regsho | — | 0.90 | No change |

### Sequencing rationale

Source weight updates are bundled into Sprint A per resolved decision 4 (phase2_plan.md section 1.5). The rationale: these weights only make sense once all articles go through a uniform scoring model (FinBERT). Updating Finnhub from 0.80 to 0.65 before FinBERT would have no effect (Finnhub articles don't score). Updating AV from 0.70 to 0.75 before FinBERT is technically safe but creates a mixed-methodology intermediate state.

### Implementation

Two-line edit in `pipeline/features/normalize.py:42,45`:
```python
"alpha_vantage": 0.75,   # was 0.70
"finnhub":       0.65,   # was 0.80
```

---

## 7. Test Coverage Impact

### Tests that reference `provider_sentiment` directly

| Test file | Test class/function | What needs to change |
|-----------|--------------------|--------------------|
| `test_sprint1_math_corrections.py:154-176` | `TestNanInfGuardsNarrative` | `_art()` helper uses `"provider_sentiment"` key → change to `"finbert_score"` |
| `test_sprint1_math_corrections.py:247-325` | `TestRelevanceThreshold` | `_art()` helper uses `"provider_sentiment"` key → change to `"finbert_score"` |
| `test_semantic_dedup.py:323-336` | `TestArticlesClusterAwareQuery.test_provider_sentiment_filter_in_where` | Asserts `"provider_sentiment IS NOT NULL"` exists in `get_articles_since` source → **must be rewritten to assert `finbert_score IS NOT NULL`** |
| `test_semantic_dedup.py:340-346` | `TestArticlesClusterAwareQuery.test_returns_expected_columns` | Asserts columns include `provider_sentiment` → update to `finbert_score` + add `finbert_pos, finbert_neg, finbert_neu` |
| `test_semantic_dedup.py:483-502` | `TestDedupIntegration.test_dedup_reduces_scoring_input` | Uses `"provider_sentiment"` key in article dicts → change to `"finbert_score"` |
| `test_per_source_halflives.py:147` | Half-life test | Uses `"provider_sentiment"` signal type label → update |

### New tests needed

| Test area | Description |
|-----------|-------------|
| `test_finbert.py` | FinBERT module: model loading, single article scoring, batch scoring, score range [-1,+1], paper example vectors |
| `test_w_conf.py` | w_conf computation: paper examples (0.92,0.05,0.03)→~0.74 and (0.40,0.35,0.25)→~0.08, edge cases (uniform distribution, degenerate) |
| `test_sprint1_math_corrections.py` | Update existing narrative tests: finbert_score key, weight formula with w_conf |
| `test_semantic_dedup.py` | Update query assertion tests for finbert_score |
| `test_language_detection.py` | Language detection: English passes, non-English excluded, detection failure handling |

### Test count impact

Current: 424 passed, 11 deselected.
Expected post-Sprint-A: ~445+ passed (existing modified + ~20 new tests).

---

## 8. Pre-Merge Production State Capture

### Baseline (2026-05-11, production API, Pro tier, full detail)

Saved to `tools/exports/sprintA_baseline_pre.json`.

| Ticker | Score | Narrative | Market | Influencer | Macro | Confidence |
|--------|-------|-----------|--------|------------|-------|------------|
| AAPL | 70 | 62.62 | 78.79 | 68.56 | 70.15 | 90 |
| MSFT | 62 | 62.40 | 55.81 | 68.86 | 70.15 | 90 |
| NVDA | 69 | 64.06 | 71.19 | 71.48 | 70.15 | 90 |
| TSLA | 57 | 51.27 | 76.50 | 31.74 | 70.15 | 65 |
| AMZN | 57 | 58.77 | 68.69 | 31.74 | 70.15 | 90 |
| JPM | 44 | 58.22 | 35.46 | 31.74 | 70.15 | 90 |
| JNJ | 50 | 63.07 | 30.11 | 56.25 | 70.15 | 75 |
| XOM | 44 | 59.25 | 32.88 | 31.74 | 70.15 | 90 |
| KO | 60 | 67.32 | 48.58 | 66.43 | 70.15 | 90 |
| BAC | 44 | 55.50 | 34.47 | 31.74 | 70.15 | 90 |

**Narrative sub-index range:** 51.27 (TSLA) to 67.32 (KO).

### Expected changes post-Sprint-A

1. **Finnhub articles enter scoring:** ~18,071 additional articles per 7-day window. Narrative sub-index will shift as these articles contribute FinBERT scores.
2. **FinBERT scores replace AV provider_sentiment:** The score mapping `50 + 50 * sent_f` remains the same, but `sent_f` comes from FinBERT's `P(pos) - P(neg)` instead of AV's `ticker_sentiment_score`. These should be directionally correlated but will differ in magnitude.
3. **w_conf dampens uncertain articles:** Articles with spread-out FinBERT probabilities (low confidence) will have near-zero weight. This effectively filters out ambiguous articles, potentially tightening the narrative sub-index toward stronger signals.
4. **Source weight changes:** AV 0.70→0.75 (mild increase), Finnhub 0.80→0.65 (significant decrease). Finnhub articles enter at lower weight than before (0.65 vs hypothetical 0.80), partially offsetting their volume.
5. **Net effect prediction:** Narrative sub-index will shift by 0-10 points for most tickers. Direction depends on Finnhub article sentiment alignment with AV sentiment. Tickers with many Finnhub-only articles (NET, VEEV, BRK.B — see section 2) may see larger shifts.

---

## 9. Open Questions for Aayudh

### No blocking questions

All architectural decisions were resolved in `phase2_plan.md` section 1.5:
- Deployment model: Phase 3 of narrative_job (resolved decision 1)
- Backfill: forward-only, script in `tools/` (resolved decision 2)
- NLP dependency: PyTorch full (resolved decision 3)
- Source weights: bundle into Sprint A (resolved decision 4)

### Implementation notes requiring awareness (not blocking)

1. **Finnhub relevance backfill.** Existing Finnhub articles in the DB have `relevance_score = NULL`. The Sprint D relevance filter (`normalize.py:443-445`) will exclude them even after FinBERT scores them. Options:
   - **(a)** Set `relevance_score = 1.0` for all Finnhub articles at ingestion going forward (1 line in `narrative.py:187`), AND run a one-time `UPDATE raw_articles SET relevance_score = 1.0 WHERE source = 'finnhub' AND relevance_score IS NULL` on the production DB.
   - **(b)** Same as (a) but skip the backfill UPDATE — only new Finnhub articles (post-deploy) will have `relevance_score = 1.0` and be eligible for scoring.

   **Recommendation:** Option (a) — the UPDATE is safe and idempotent. ~18k rows affected.

2. **FinBERT model download on first deploy.** The model downloads on first `narrative_job` run (~440 MB). This adds ~30-60 seconds to the first job execution. Subsequent runs use the cached model. No action needed unless Railway's ephemeral filesystem clears the cache on restart — in that case, consider adding `HF_HOME` to a persistent volume.

3. **`provider_sentiment` column preservation.** The column remains in the schema but is no longer used for scoring. It retains historical AV-sourced sentiment for audit/comparison purposes. No schema change needed.

---

## Implementation Checklist (Phase 2 — for reference only, not started)

Per the sprint prompt, these are the implementation steps to execute after diagnostic approval:

1. Schema migration 007 (finbert_pos, finbert_neg, finbert_neu, language)
2. `pipeline/nlp/finbert.py` — model loading + inference
3. Language detection at ingestion (`narrative.py`)
4. New DB queries (`get_unscored_articles`, `update_finbert_scores`)
5. Modify `get_articles_since()` — switch from `provider_sentiment` to `finbert_score`
6. Modify `score_narrative_signals()` — use `finbert_score`, add `w_conf`
7. Update source weights (AV 0.75, Finnhub 0.65)
8. Add Phase 3 to `narrative_job` in scheduler
9. Finnhub `relevance_score = 1.0` at ingestion
10. Update existing tests + write new tests
11. Backfill script (`tools/backfill_finbert.py`)
12. CHANGELOG update

---

## Anomalies

1. **10 tickers have Finnhub-only coverage (no AV articles with sentiment) in the last 3 days.** Notable: NET (9 articles), VEEV (7), BRK.B (6). These tickers currently have zero narrative signal from their Finnhub articles. Post-Sprint-A, they gain narrative coverage immediately.

2. **`finbert_score` column already exists** (from original `migrations.sql`). Migration 007 only needs to add the three class probability columns and language. No need to re-add `finbert_score`.

3. **`sentence-transformers==3.0.0` in requirements.txt already depends on `torch`.** Adding `torch` explicitly to requirements.txt is for version pinning, not a new transitive dependency. The actual new top-level dependencies are `transformers` and `langdetect`.
