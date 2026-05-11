# Phase 2 Sprint Plan — Make the Paper a Reality

**Generated:** 2026-05-11
**Source checklist:** masterchecklist.md as of 2026-05-11
**Diagnostic reference:** docs/diagnostic_narrative_2026_05_11.md
**Research paper:** docs/Research Paper (Research Doc) (3).pdf
**Status:** DRAFT

---

## 1. Executive Summary

Phase 2 closes the gap between the deployed Phase 1 implementation and the research paper's specification. The primary deliverable is a fully paper-compliant Narrative channel: local FinBERT scoring replacing Alpha Vantage's external NLP, model-confidence weighting (`w_conf`), two new data sources (ApeWisdom retail sentiment and SEC EDGAR 8-K filings), a tightened relevance threshold (0.10 → 0.60), and per-source half-life configuration. Secondary deliverables include paper text reconciliation (six specific edits), non-narrative channel improvements (macro cadence, insider author credibility, FRED rates), and bootstrap confidence intervals. When complete, the deployed system will match every formula, source, weight, and threshold stated in the paper's Narrative Signals section, and the paper text will match the deployed code where code is canonical.

---

## 1.5 Resolved Decisions (Pre-Sprint)

Decisions locked through the planning conversation on 2026-05-11. These supersede the corresponding Open Questions in section 5 of this document.

**FinBERT (Sprint A):**
1. **Deployment model:** In-pipeline, Phase 3 of `narrative_job`. No separate `finbert_job`.
2. **Backfill:** Forward-only. One-off backfill script available in `tools/` for manual use; not run as part of Sprint A.
3. **NLP dependency:** PyTorch full (`transformers` + `torch`). ONNX deferred unless Railway memory pressure emerges.
4. **Source weight updates:** Bundle into Sprint A. AV `0.70 → 0.75`, Finnhub `0.80 → 0.65` per paper.

**ApeWisdom (formerly Sprint B):**
5. **Deferred to Future Additions.** ApeWisdom does not return a sentiment score per the spike — it returns mention volume and upvote counts only, which is an attention signal rather than a sentiment signal. Including it in narrative as a sentiment source would be methodologically muddled. Sprint B is removed from Phase 2 scope. Direct Reddit/X integration with FinBERT post-level scoring is the long-term path; see paper Future Additions.

**EDGAR 8-K (Sprint C):**
6. **Content extraction:** Full filing body, strip HTML, truncate to 512 tokens for FinBERT. Item-level extraction deferred.
7. **Scheduler offset:** Run 8-K job offset by 15 minutes from existing EDGAR Form 4 job to avoid rate-limit contention on the shared EDGAR_SEM(5) semaphore.

**General:**
8. **Parallel sprints:** No. One sprint at a time per practical sprintrules interpretation, despite parallel-safety analysis.

**Pre-sprint verifications completed 2026-05-11:**
- **Railway capacity:** Hobby plan confirmed, 8 GB RAM per service, ample headroom for FinBERT (~500 MB resident addition).
- **Relevance threshold coverage analysis:** 84% of Alpha Vantage articles in last 7 days survive the 0.60 threshold (Railway DB query 2026-05-11). Sprint D can ship independently of Sprint A — coverage drop is small.
- **ApeWisdom API spike:** Completed at `docs/spikes/apewisdom_2026_05_11.md`. API healthy but returns mention counts only, no sentiment field. ApeWisdom moved to Future Additions on this basis.

**Sprint sequencing locked:**
1. Sprint D (relevance threshold tightening to 0.60) — warm-up
2. Sprint A (FinBERT + w_conf + source weights bundle)
3. ~~Sprint B (ApeWisdom)~~ — dropped, deferred to Future Additions
4. Sprint C (EDGAR 8-K)
5. Sprint 2.10 (data retention policy)
6. Sprint 2.9 (bootstrap confidence intervals)
7. Sprint E (paper edits) — already completed 2026-05-11

---

## 2. Per-Item Plan

### 2.1 — FinBERT Integration (Paper Stage 3)

**What this implements from the paper:**

Paper "Stage 3: Sentiment scoring" (line 1128–1132 in extracted text):
> "Articles surviving validation and relevance filtering are passed through FinBERT (ProsusAI/finbert), a transformer-based sentiment model fine-tuned on financial text. Provider-supplied sentiment scores from Alpha Vantage are not used; running every article through a single documented model ensures methodological consistency and auditable, reproducible scores across all textual sources."

Paper "Scoring for Textual Events" formula (line 238):
> S_i = P_i(positive) − P_i(negative)

This produces a continuous score in [-1, +1] from the three-class softmax output.

**Current deployed state:**

- All narrative sentiment comes from AV's `ticker_sentiment_score` field, parsed at `pipeline/sources/narrative.py:109` and stored in `raw_articles.provider_sentiment`.
- Finnhub articles stored with `provider_sentiment=None` (`narrative.py:186`), excluded by `WHERE provider_sentiment IS NOT NULL` at `db/queries/raw_articles.py:128`.
- `finbert_score` column exists in schema (`migrations.sql:39`) — all NULLs.
- `finbert_pos`, `finbert_neg`, `finbert_neu` columns do **not** exist.
- `language` column does **not** exist.
- No `pipeline/nlp/finbert.py` file exists. The `pipeline/nlp/` directory contains only `__init__.py` and `dedup.py`.
- Score mapping at `normalize.py:449`: `score = 50.0 + 50.0 * sent_f` — linear transform from [-1,+1] to [0,100].
- Weight formula at `normalize.py:452`: `weight = _source_weight(source) * w_time * relevance` — no `w_conf` term.

**What needs to change:**

| Change | File | Scope |
|--------|------|-------|
| Schema migration: add `finbert_pos`, `finbert_neg`, `finbert_neu`, `language` columns | `migrations/007_finbert_columns.sql` | New file |
| New FinBERT module: model loading (lazy), single-article inference, batch inference | `pipeline/nlp/finbert.py` | New file (~150 lines) |
| New language detection at ingestion | `pipeline/sources/narrative.py` | Add langdetect/fasttext call before insert |
| Add Phase 3 (FinBERT batch scoring) to narrative_job | `pipeline/scheduler.py` | Modify `narrative_job()` (~30 lines added) |
| New DB function: update finbert columns for scored articles | `db/queries/raw_articles.py` | Add `update_finbert_scores()` function |
| New DB function: fetch unscored articles for FinBERT batch | `db/queries/raw_articles.py` | Add `get_unscored_articles()` function |
| Modify `get_articles_since()`: remove `provider_sentiment IS NOT NULL` gate, use `finbert_score` | `db/queries/raw_articles.py` | Modify existing query |
| Modify `score_narrative_signals()`: use `finbert_score` instead of `provider_sentiment` | `pipeline/features/normalize.py` | Modify scoring function |
| Update source weights to paper values: AV 0.70→0.75, Finnhub 0.80→0.65 | `pipeline/features/normalize.py` | 2-line edit in `_SOURCE_WEIGHTS` |
| Tests: FinBERT module, scoring with finbert_score, Finnhub articles now contributing | `tests/` | New + modified test files |

**Dependencies and blockers:**

- **External:** Requires `transformers` and `torch` (or `onnxruntime`) as new dependencies. ProsusAI/finbert model download (~440 MB). Railway deployment must support model file.
- **Schema:** Migration 007 must land before any FinBERT scoring code runs. The migration is additive (new nullable columns) — no downtime risk.
- **No Phase 2 blockers:** This is the root item; 2.2 and partially 2.5 depend on it.

**Risk and verification:**

| Risk | Mitigation |
|------|------------|
| FinBERT CPU inference latency | Only NEW articles need scoring per narrative_job run. At ~50-200 new articles per 30-min window × ~100ms/article on CPU = 5-20 seconds. Well within the 30-min job budget. If volume spikes, batch inference with padding handles it. |
| Model memory footprint (~500 MB) | Railway's smallest container has 512 MB. May need 1 GB plan. Lazy-load model on first use (same pattern as `dedup.py:49-56` for sentence-transformers). |
| AV sentiment scores disappear as primary | Verify that FinBERT scores for the same articles are directionally correlated with AV scores before switching. Run a 7-day parallel period where both are computed but only FinBERT is used. |
| Finnhub articles flood the narrative index | Finnhub articles enter with `w_rel = 1.0` per paper (ticker-keyed endpoint). Monitor article count and sub-index volatility. |

**Verification:** 10-ticker baseline before/after. Count of articles contributing to narrative per ticker (expect increase from Finnhub inclusion). FinBERT score distribution histogram vs AV provider_sentiment distribution.

**Effort:** **Large.** New module, schema migration, scoring path rewrite, 8+ files touched, requires diagnostics phase per sprintrules.

---

### 2.2 — w_conf from FinBERT Class Probabilities (Paper Event-Level Weighting)

**What this implements from the paper:**

Paper "Event-level Weighting" section (lines 1170–1185):
> "FinBERT outputs three probabilities — positive, neutral, negative — that sum to 1. When one class dominates (e.g. P_pos = 0.92), the model is confident; when the probabilities are spread evenly (e.g., 0.35, 0.35, 0.30), the model is uncertain."

Inverse normalized entropy formula:
> w_conf = 1 − (−Σ P_i(k) ln P_i(k)) / ln(3)

Paper example: FinBERT output (0.92, 0.05, 0.03) → w_conf ≈ 0.74; output (0.40, 0.35, 0.25) → w_conf ≈ 0.08.

For ApeWisdom: w_conf = 1.0 at ingestion (line 1183).

Target weight formula per paper:
> w_i = w_src · w_rel · w_conf · e^(−λΔt_i)

(Note: paper's general formula includes w_author, but per checklist item 2.8 note, narrative w_author is folded into w_src.)

**Current deployed state:**

- Weight formula at `normalize.py:452`: `weight = _source_weight(source) * w_time * relevance`
- No `w_conf` term. No class probabilities stored (columns don't exist yet).
- After 2.1 ships, `finbert_pos/neg/neu` columns will exist and be populated.

**What needs to change:**

| Change | File | Scope |
|--------|------|-------|
| Add `_compute_w_conf(pos, neg, neu)` helper function | `pipeline/features/normalize.py` | ~10 lines |
| Modify weight formula in `score_narrative_signals()` | `pipeline/features/normalize.py` | Add `* w_conf` to weight computation |
| Modify `get_articles_since()` to also return `finbert_pos, finbert_neg, finbert_neu` | `db/queries/raw_articles.py` | Add columns to SELECT |
| Tests: w_conf computation, paper examples as regression tests | `tests/` | New tests |

**Dependencies:** **Blocked by 2.1** — requires class probability columns populated by FinBERT.

**Risk:** Low. The entropy formula is deterministic and testable against paper examples. Main concern is that highly uncertain articles (w_conf ≈ 0.08) will contribute almost nothing, which may reduce effective signal count and trigger shrinkage. Monitor n_signals in sub-index after deployment.

**Verification:** Compute w_conf for the paper's example vectors and verify exact match. 10-ticker baseline showing narrative sub-index shift from w_conf introduction.

**Effort:** **Small.** Ships as second half of the FinBERT sprint. ~4 files, all modifications, clear formula, well-scoped.

---

### 2.3 — ApeWisdom Source (DEFERRED to Future Additions)

**Status:** Removed from Phase 2 scope on 2026-05-11 based on spike findings.

**Reason:** The ApeWisdom API returns mention volume and upvote counts only, not a sentiment score. Treating volume as sentiment would be methodologically incorrect. See `docs/spikes/apewisdom_2026_05_11.md` for details.

**Future path:** Direct Reddit/X integration with FinBERT post-level scoring is the longer-term replacement, captured in paper Future Additions. ApeWisdom or equivalent may be revisited as a separate attention signal (distinct from sentiment) once narrative validation is complete.

---

### 2.4 — SEC EDGAR 8-K Source (Paper Data Sources Row 3)

**What this implements from the paper:**

Paper data table Row 3 (lines 1004–1024):
> "Press releases — Company Issued and material filings — SEC EDGAR 8-K — Captures the company's own voice on material events such as earnings, guidance changes, M&A, and product launches"

Paper source weight: 1.00 (line 1154). Half-life: 168 hours / 7 days (line 1198). w_rel = 1.0 by construction (lines 1120–1122):
> "SEC EDGAR 8-K filings are filed by the company itself against a specific Central Index Key (CIK). Each filing is unambiguously about exactly one issuer, and these enter the pipeline at w_rel = 1.0 by construction."

**Current deployed state:**

- SEC EDGAR integration exists for Form 4 (insider trades) at `pipeline/sources/influencer.py:103-204`.
- Shared utilities available: CIK cache (`_load_cik_map`, `_get_cik`) at `influencer.py:59-80`, EDGAR headers at `influencer.py:53-57`, rate limits at `rate_limits.py:51-52` (`EDGAR_SEM = Semaphore(5)`, `EDGAR_DELAY = 0.5`).
- SEC User-Agent already configured: `"SentimentAPI admin@sentimentapi.local"` (`influencer.py:55`).
- No `pipeline/sources/edgar_8k.py` exists.
- No EDGAR 8-K scheduler job exists.
- Source weight `"sec_edgar": 1.0` already exists in `_SOURCE_WEIGHTS` (`normalize.py:44`) — reusable for 8-K.

**Is EDGAR 8-K self-contained?** **Mostly yes.** The new fetcher would import the CIK lookup utility and EDGAR constants/headers from `influencer.py` (or from a shared module extracted from it). The EDGAR rate limiter pool is shared (5 concurrent slots) which means 8-K and Form 4 requests compete for the same slots — this is correct per SEC fair-access policy. No modifications to the existing Form 4 code are needed.

**What needs to change:**

| Change | File | Scope |
|--------|------|-------|
| New 8-K fetcher | `pipeline/sources/edgar_8k.py` | New file (~120 lines). Fetch submissions JSON, filter for 8-K/8-K/A forms, extract filing body. |
| Extract shared EDGAR utilities (optional) | `pipeline/sources/influencer.py` → `pipeline/sources/_edgar_common.py` | Refactor CIK cache and headers to shared module. Or just import from influencer.py. |
| New scheduler job | `pipeline/scheduler.py` | Add `edgar_8k_job()` + registration |
| Add half-life override 168h | `pipeline/features/normalize.py` | 1-line edit: `("narrative", "edgar_8k"): 168.0` |
| Set relevance=1.0 at ingestion | `pipeline/sources/edgar_8k.py` | Hard-code `relevance_score=1.0` at insert |
| Insert 8-K articles to `raw_articles` with `source='edgar_8k'` | `db/queries/raw_articles.py` | Reuse existing `insert_article()` |
| Tests | `tests/` | New test file |

**Dependencies:** If FinBERT (2.1) is not yet wired, 8-K articles will have `finbert_score=NULL` and won't be scored (same as current Finnhub situation). **Ideally ships after or concurrent with 2.1** so 8-K articles are immediately scored by FinBERT. If shipped before 2.1, articles are stored but unscored until FinBERT lands.

**Risk:**

| Risk | Mitigation |
|------|------------|
| 8-K content extraction complexity | Start with full filing body. Item-level extraction (Item 2.02, 5.02, etc.) is a refinement — decision needed. |
| Low 8-K volume (most companies file 1-2/month) | Expected behavior. 8-K's value is high weight (1.00) and long half-life (7 days), not volume. |
| SEC rate limiting (10 req/s policy) | Reuse existing EDGAR_SEM(5) + 0.5s delay — already within policy. |
| SGML/XML parsing failures | Same pattern as Form 4 — expected and logged at DEBUG per CLAUDE.md. |

**Verification:** Check 8-K article count in `raw_articles` after first job run. Verify `source='edgar_8k'`, `relevance_score=1.0`. Verify time decay: 8-K from 3 days ago should still carry ~74% weight (vs 12.5% for a news article).

**Effort:** **Medium.** New source file + scheduler job, but reuses existing EDGAR infrastructure. ~5 files. Decision needed on content extraction approach.

---

### 2.5 — Relevance Threshold Tightening to 0.60 (Paper Stage 2)

**What this implements from the paper:**

Paper Stage 2 (lines 1096–1112):
> "The framework distinguishes between direct narrative — content whose primary subject is the target asset — and indirect narrative — content that mentions the target asset in a broader context. Only direct narrative contributes to the focal ticker's narrative sub-index at full weight."

> "The system applies a threshold of 0.60: articles meeting this bar are treated as direct narratives for the corresponding ticker, while articles below the threshold are excluded from that ticker's scoring cycle."

**Current deployed state:**

- Threshold: `if relevance < 0.1: continue` at `normalize.py:444`.
- Default for missing relevance: `relevance = float(art.get("relevance_score") or 0.5)` at `normalize.py:443`.
- This means articles without explicit `relevance_score` default to 0.5 and pass the 0.1 threshold.

**What needs to change:**

| Change | File | Scope |
|--------|------|-------|
| Change threshold 0.1 → 0.6 | `pipeline/features/normalize.py:444` | 1-line edit |
| Change missing-relevance default from 0.5 to exclusion | `pipeline/features/normalize.py:443` | Change `or 0.5` to `or 0.0` (excluded by threshold) or explicit `if relevance_score is None: continue` |
| Update tests | `tests/` | Modify threshold-related test assertions |

**Is 2.5 "must follow A" or can it ship sooner?**

**Code coupling:** Zero. The relevance threshold operates on AV's `relevance_score` field, which exists independently of FinBERT. The threshold change is a single-line edit that works identically with `provider_sentiment` or `finbert_score`.

**Risk coupling:** Moderate. Tightening from 0.10 to 0.60 will exclude a substantial fraction of AV articles. Before FinBERT (2.1), Finnhub articles can't compensate (still excluded by `provider_sentiment IS NOT NULL`). After FinBERT, Finnhub articles enter the scoring path and partially offset the AV article drop. Shipping 2.5 before 2.1 risks a larger coverage hole.

**Recommendation:** 2.5 **can** ship independently but **should** follow 2.1 unless coverage analysis shows the drop is tolerable. The sprint should include a pre/post article-acceptance rate analysis for 10 tickers, as the checklist requires.

**Risk:**

| Risk | Mitigation |
|------|------------|
| Coverage drop: many tickers lose narrative entirely | Monitor article-acceptance rates pre/post. If >15% of tickers lose all narrative, escalate before merging. |
| Small-cap tickers systematically affected | AV returns fewer articles for small-caps, and those articles may have lower relevance. Document patterns. |

**Verification:** Before/after article-acceptance counts for 10-ticker baseline. List of tickers that drop to zero narrative coverage. Distribution of relevance_score values in recent articles (to predict the drop empirically).

**Effort:** **Small.** 2-line code change + test updates + coverage monitoring. Behavioral impact is the main work.

---

### 2.6 — Per-Source Half-Lives Verification (Paper Event-Level Weighting)

**What this implements from the paper:**

Paper half-life table (lines 1190–1224):

| Source | Half-Life | Code Location |
|--------|-----------|---------------|
| SEC EDGAR 8-K | 168 hours (7 days) | Covered by 2.4: `_HALF_LIFE_OVERRIDE[("narrative", "edgar_8k")] = 168.0` |
| Alpha Vantage | 12 hours | Already deployed: `_LAYER_HALF_LIFE_H["narrative"] = 12.0` (`normalize.py:62`) |
| Finnhub | 12 hours | Already deployed via layer default (same as AV). Paper half-life cell is blank — checklist 2.7 says edit paper to show 12h explicitly. |
| ApeWisdom | 6 hours | Covered by 2.3: `_HALF_LIFE_OVERRIDE[("narrative", "apewisdom")] = 6.0` |

**Is 2.6 verification-only or does it need code changes?**

**Verification-only.** All code changes are covered by 2.3 (ApeWisdom override) and 2.4 (EDGAR override). AV and Finnhub already have the correct 12h half-life via the layer default. Item 2.6 is a post-deployment confirmation step, not a separate sprint.

**Verification approach:** After 2.3 and 2.4 ship, query articles at known ages and verify their computed weights match the expected decay curve. Specifically:
- A 12h-old AV article should have weight ≈ 50% of a fresh one.
- A 3.5-day-old 8-K filing should have weight ≈ 50% of a fresh one.
- A 6h-old ApeWisdom signal should have weight ≈ 50% of a fresh one.

**Effort:** Not a sprint. Verification step folded into the post-deployment checks for 2.3 and 2.4.

---

### 2.7 — Paper Reconciliation (Six Specific Edits)

**What this implements:** Alignment between paper text and deployed code where they currently disagree. Per checklist, code is canonical for each of these items — the paper is edited to match.

| # | Discrepancy | Paper Currently Says | Paper Should Say | Code Reference |
|---|-------------|---------------------|-----------------|----------------|
| 1 | Shrinkage factor | Not mentioned | Add to Aggregation section: "A volume-shrinkage factor min(1, n/5) pulls the sub-index toward neutral when fewer than 5 events are present, preventing single-event scoring cycles from producing extreme values." | `subindices.py:73` |
| 2 | Validation paragraph in Stage 1 | Mentions "validation and deduplication" but only explains dedup | Expand to describe validation checks: non-empty body, valid timestamp, identifiable source | `narrative.py:96-101` (AV timestamp check), `narrative.py:167-168` (Finnhub URL/timestamp check) |
| 3 | Future Additions block | "Add Options data, Add company specific macro data" | Replace with narrative-specific: earnings call transcripts, direct Reddit/X integration, sarcasm detection, indirect narrative routing, per-aspect dimensions | Paper lines 1249–1252 |
| 4 | α_narrative value | Referenced but not stated | Add "= 0.30" explicitly | `composite.py:25` |
| 5 | Finnhub half-life | Cell appears blank in table | Add "12 hours" explicitly | `normalize.py:62` (layer default) |
| 6 | Dedup cosine threshold | 0.92 | 0.85 (production-deployed, empirically tuned) | `dedup.py:45` |

**Dependencies:** None. Paper edits don't depend on any code sprint.

**Risk:** Low. These are documentation changes. Only risk is introducing an inconsistency while editing.

**Effort:** **Not a code sprint.** This is a paper-editing task. Can be done at any time.

---

### 2.8 — Non-Narrative Channel Improvements

These are individually scoped items not on the Narrative critical path. Each is a separate small-to-medium sprint.

#### G-S17 — Hourly Macro Ingestion

**Paper:** Data Collection table specifies macro signals at hourly cadence during trading hours.
**Current:** `macro_job` runs daily at 02:00 UTC via `CronTrigger(hour=2, minute=0)` (scheduler.py:607-608).
**Change:** Modify trigger to hourly during trading hours + daily overnight.
**Effort:** Small.

#### G-S18 — Separate Insider Job

**Paper:** Insider signals at hourly cadence.
**Current:** `influencer_job` runs every 6 hours, combining Form 4 insider + analyst signals.
**Change:** Split into `insider_job` (hourly) and `analyst_job` (every 6h). The existing `fetch_influencer_signals()` in `influencer.py` would need to be split.
**Effort:** Medium. Refactoring existing code.

#### G-S12 — Insider Author Credibility (w_author for Influencer Channel)

**Paper:** w_author weight for insider transactions based on role (CEO > VP > board member).
**Current:** No `insider_role` or `w_author` in the influencer scoring path. Form 4 XML parsing (`influencer.py:165-180`) extracts transaction data but not the relationship field.
**Change:** Extract `isOfficer`/`officerTitle`/`isDirector` from Form 4 XML. Add schema column. Implement credibility heuristic.
**Effort:** Medium.

#### G-S19, G-S20, G-S21, G-S22 — Additional Signal Sources

These require external data source decisions (Finnhub paid tier, FRED API, etc.) and are not narrative-related. Each is a Medium sprint when the data source decision is made.

#### G-K10 — Macro Stock-Specificity

**Paper:** Treats this as Future Extensions.
**Current:** Macro sub-index uses uniform VIX + sector ETF basket for all tickers.
**Change:** Weight sector ETFs per ticker's sector from `ticker_universe.sector`.
**Effort:** Medium. Optional for Phase 2.

---

### 2.9 — Bootstrap Confidence Intervals

**What this implements from the paper:** Statistical confidence intervals alongside the existing operational confidence score.

**Current deployed state:**

- Operational confidence at `pipeline/confidence/scorer.py:30-35`: starts at 100, subtracts penalties (missing_layer −15, stale_source −10, low_signal_volume −20, high_divergence −15).
- No `confidence_interval_low` or `confidence_interval_high` columns in `sentiment_history`.

**What needs to change:**

| Change | File | Scope |
|--------|------|-------|
| Schema migration: add CI columns | `migrations/008_confidence_intervals.sql` | New file |
| Bootstrap CI computation | `pipeline/confidence/bootstrap.py` | New file (~80 lines) |
| Wire CI computation into scoring path | `pipeline/orchestrator.py` | ~10 lines in `_score_and_write()` |
| Persist CI values | `pipeline/persistence/pg_writer.py` | Add columns to INSERT |
| Tests | `tests/` | New test file |

**Dependencies:** None from other Phase 2 items. Can ship independently.

**Risk:** Bootstrap is computationally expensive if run on every scoring tick for 502 tickers. Mitigation: sample-based bootstrap (100-500 resamples) and/or compute CIs only when n_signals > threshold.

**Effort:** **Medium.** New module + schema + wiring. ~5 files.

---

## 3. Sprint Sequencing

### Locked Sequence

```
Sprint D    [Small]   2.5 (Relevance threshold)   ← warm-up, ships first
Sprint A    [Large]   2.1 (FinBERT) + 2.2 (w_conf) + source weight updates
Sprint C    [Medium]  2.4 (EDGAR 8-K)             ← scheduler offset from Form 4
Sprint 2.10 [Med]     Data retention policy
Sprint 2.9  [Med]     Bootstrap CIs
        ── 2.6 verification window after A and C deploy ──
Sprint F    [varies]  2.8 items (non-narrative)   ← independent; schedule per priority
```

Sprint B (ApeWisdom) removed — see section 1.5. Sprint E (paper edits) already completed 2026-05-11.

### Sequencing Rationale

**Sequence locked 2026-05-11.** One sprint at a time per practical sprintrules interpretation.

**Sprint D ships first as warm-up.** Coverage analysis confirmed 84% of AV articles survive the 0.60 threshold (Railway DB query 2026-05-11). The coverage drop is small enough to ship independently of FinBERT. This is a 2-line code change with well-understood behavioral impact, making it an ideal first Phase 2 sprint.

**Sprint A is the critical path.** FinBERT + w_conf is the largest item and unblocks EDGAR 8-K scoring (articles stored by Sprint C need FinBERT to contribute to the narrative sub-index). It is the only Large sprint and requires a diagnostics phase per sprintrules. Combining 2.1 and 2.2 into one sprint is justified because w_conf is a ~10-line addition to the weight formula that directly depends on the class probability columns created by 2.1 — splitting them would mean two separate rounds of schema migration, query modification, and test updates for the same data path. Source weight updates (AV 0.70→0.75, Finnhub 0.80→0.65) are bundled into Sprint A since they only make sense once all articles go through a uniform scoring model.

**Sprint C (EDGAR 8-K) follows A.** 8-K articles need FinBERT to be scored. The 8-K scheduler job is offset by 15 minutes from the existing EDGAR Form 4 job to avoid rate-limit contention on the shared EDGAR_SEM(5) semaphore.

**Sprint E (paper edits) is complete.** Six paper reconciliation edits were made 2026-05-11.

**Item 2.6 is not a sprint.** It is a verification step confirming half-life behavior after Sprint C (EDGAR 8-K) deploys. The only code involved — adding `_HALF_LIFE_OVERRIDE` entries — is covered within Sprint C. AV and Finnhub already have the correct 12h half-life via the layer default.

---

## 4. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| **R1** | **FinBERT CPU inference latency exceeds 30-min job budget.** At 502 tickers × N articles, batch scoring in `narrative_job` Phase 3 could run long. | Low | High | Only NEW (unscored) articles need scoring per job run. Typical volume: 50-200 new articles/run × ~100ms/article = 5-20 seconds. If volume spikes to 1000+, batch with padding. If systematically slow, move FinBERT to a separate background job with lower cadence. |
| **R2** | **Coverage drop from relevance 0.10→0.60.** Unknown fraction of AV articles fall between 0.10 and 0.60 relevance. Could leave many tickers with zero narrative coverage. | Medium | High | Run the coverage query (relevance distribution) BEFORE the sprint. If >20% of tickers lose all narrative, reconsider threshold or time the sprint after Finnhub articles can compensate (post-FinBERT). Checklist explicitly says "do NOT silently revert." |
| **R3** | **ApeWisdom API availability, rate limits, or response format.** API documentation is sparse. May not support per-ticker queries at the cadence and scale needed (502 tickers). | Medium | Medium | Research API before sprint starts. ApeWisdom's public endpoint returns a ranked list of all trending stocks in one call, not per-ticker queries. If so, one API call per job run suffices. If API is unavailable or unreliable, the paper's retail signal is deferred (ApeWisdom is the lowest-weight source at 0.30). |
| **R4** | **Railway memory constraints for FinBERT model.** ProsusAI/finbert requires ~440 MB for model weights plus tokenizer. Railway's smallest containers may not have headroom. | Medium | High | Check Railway plan memory limits. If insufficient, options: (a) upgrade Railway plan, (b) use ONNX-quantized variant of FinBERT, (c) run FinBERT on a separate service. Lazy-loading (same pattern as `dedup.py` sentence-transformers) avoids loading until first use. |
| **R5** | **Schema migration ordering.** Migrations 007 (finbert columns) and 008 (CI columns) must not conflict. If both add columns to different tables, they're safe. If both touch the same table, ordering matters. | Low | Low | Migration 007 adds columns to `raw_articles`. Migration 008 adds columns to `sentiment_history`. Different tables — no conflict. Apply in numerical order. |
| **R6** | **SEC EDGAR 8-K content extraction quality.** 8-K filings contain structured exhibits (HTML, PDF) wrapped in SGML. Extracting clean text for FinBERT is non-trivial. | Medium | Medium | Start with the filing body text (the exhibit file), strip HTML tags, truncate to FinBERT's 512-token window. Item-level extraction (targeting specific 8-K Items like 2.02, 5.02) is a refinement. Per existing EDGAR pattern in `influencer.py`, XML parse failures are expected and logged at DEBUG. |
| **R7** | **FinBERT backfill cost.** If backfilling historical articles (where `finbert_score IS NULL`), the one-time batch could be 10,000+ articles. At ~100ms/article on CPU: ~17 minutes. | Low | Low | Forward-only is acceptable per checklist ("forward-only acceptable if backfill cost is prohibitive"). If backfilling, run as a one-time script outside the scheduled job. Not blocking. |

---

## 5. Open Questions — SUPERSEDED

The questions below were posed in the original plan. All have been resolved in section 1.5 above. Retained for historical context only.

### Must resolve before Sprint A (FinBERT)

1. **FinBERT deployment model: in-pipeline at ingestion vs. batched background job?**
   Recommendation: **Phase 3 of narrative_job** (after fetch + cluster). This keeps the architecture simple (no new job), ensures articles are scored before the next scoring_tick_job, and allows efficient batching. The alternative (separate `finbert_job` with its own cadence) offers scheduling flexibility but adds complexity.

2. **FinBERT backfill: forward-only or backfill historical?**
   The checklist says "forward-only acceptable if backfill cost is prohibitive." At ~10,000 historical articles × 100ms = ~17 minutes one-time batch. Recommendation: forward-only for initial deploy, with a one-off backfill script available to run manually if desired.

3. **New dependencies: `transformers` + `torch` (or `onnxruntime`)?**
   ProsusAI/finbert requires HuggingFace `transformers`. PyTorch is the default backend (~2 GB install). ONNX Runtime is smaller (~200 MB) if the model is exported to ONNX format. Decision: PyTorch (simpler, ProsusAI provides native weights) or ONNX (smaller, faster on CPU)?

4. **Source weight reconciliation: update AV from 0.70→0.75 and Finnhub from 0.80→0.65 as part of Sprint A?**
   The paper specifies these values for the post-FinBERT architecture. Current code values were set during Phase 1 when AV was the only scoring source. Recommendation: yes, update as part of Sprint A since the context changes.

### Must resolve before Sprint B (ApeWisdom)

5. **ApeWisdom API: which specific endpoint, auth requirements, rate limits?**
   The public endpoint appears to be `https://apewisdom.io/api/v1.0/filter/all-stocks/`. If it returns a ranked list of all stocks in one call (not per-ticker), the integration is simpler than expected — one API call per job run, parsing the response for S&P 500 tickers. Need to verify: response format, update frequency, and whether the API is stable/maintained.

6. **ApeWisdom z-score window: 200 observations per paper. At what cadence?**
   If the job runs every 30 minutes: 200 obs = ~4.2 days to fill. If hourly: 200 obs = ~8.3 days. Cadence should match ApeWisdom's own update frequency (if they update hourly, running every 30 min wastes API calls). Need: ApeWisdom's actual data refresh rate.

### Must resolve before Sprint C (EDGAR 8-K)

7. **8-K content extraction approach: full filing body, Item-level, or exhibit-only?**
   Recommendation: start with **full filing body** (strip HTML, truncate). Refine to Item-level extraction later if FinBERT accuracy improves with cleaner input. Exhibit-only is fragile (exhibit naming varies).

### General

8. **Parallel sprints allowed?**
   Sprintrules say each sprint runs on its own branch off main. Parallel sprints are technically feasible (different branches, non-overlapping file changes), but sprintrules don't explicitly authorize concurrent sprints. If parallel is permitted, B and C can run during A's diagnostics phase. If strict one-at-a-time, the sequence is A → B → C → D.

---

## 6. Out-of-Scope Reminder

These items were explicitly removed from Phase 2 in the updated checklist. They should NOT be picked up by any Phase 2 sprint.

| Item | Where Relocated | Why Not Phase 2 |
|------|----------------|-----------------|
| **ABSA (aspect-based sentiment)** | Paper Future Additions, Phase 3+ | Paper does not list ABSA as a Phase 2 target. Requires sub-aspect schema and model work beyond FinBERT. |
| **Earnings call transcripts** | Paper Future Additions | Distinct from EDGAR 8-K. 8-K filings are company disclosures; earnings call transcripts are spoken-word recordings requiring ASR + NLP. Different data source, different pipeline. |
| **Direct Reddit / PushShift / Twitter-X integration** | Paper Future Additions | ApeWisdom (2.3) provides the retail signal for Phase 2. Full textual Reddit/X access supersedes ApeWisdom later but requires separate infrastructure (streaming API, text scoring, sarcasm detection). |
| **Narrative w_author as standalone term** | Removed entirely | Per paper, narrative w_author is folded into w_src for institutional and retail sources. Only the Influencer channel needs w_author as a standalone term (insider role), tracked in 2.8. |

---

## Recommended First Sprint

**Sprint A (FinBERT + w_conf) should run first.** It is the critical path for the entire Phase 2 narrative work: it unblocks Finnhub articles for scoring (resolving the single largest coverage gap identified in the diagnostic), enables w_conf weighting (completing the paper's event-level weight formula), and establishes the local NLP infrastructure that EDGAR 8-K articles will use when Sprint C lands. Every other narrative sprint benefits from FinBERT being deployed — ApeWisdom (B) is parallel-safe but benefits from a fuller narrative index for comparison baselines; EDGAR 8-K (C) articles need FinBERT to be scored; relevance tightening (D) is less risky when Finnhub articles compensate. Sprint A is Large and requires a diagnostics phase, so it has the longest lead time — starting it first maximizes overall throughput.
