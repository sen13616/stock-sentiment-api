# Changelog

## Phase 2

### Sprint A — FinBERT integration + w_conf + source weight reconciliation (2026-05-11)

Sprint A: All narrative articles now scored by ProsusAI/finbert (paper Stage 3). AV's provider_sentiment retired from scoring path. Finnhub articles now contribute to narrative sub-index (~18k/week unlocked). Weight formula updated to paper spec: w_i = w_src * w_rel * w_conf * e^(-lambda * dt). Source weights reconciled: AV 0.70→0.75, Finnhub 0.80→0.65. Language detection (langdetect) at ingestion filters non-English articles. Schema migration 007 adds finbert_pos/neg/neu + language columns.

### Sprint D — Relevance threshold tightening (2026-05-11)

Sprint D: Narrative relevance threshold tightened from 0.10 to 0.60 per paper Stage 2. Articles without explicit relevance_score now excluded rather than defaulted. ~16% reduction in AV article volume per coverage analysis 2026-05-11.

## Phase 1

### Sprint 7 — Phase 1 methodology compliance gate (2026-05-10)
### Sprint 6 — Semantic deduplication wired into narrative path (2026-05-10)
### Sprint 5a — EMA smoothing on composite score (2026-05-09)
### Sprint 4 — Z-score normalization + per-source half-lives (2026-05-08)
### Sprint 3 — Global scoring tick (2026-05-07)
### Sprint 2 — Volume ratio in market sub-index (2026-05-07)
### Sprint 1 — Math correctness fixes (2026-05-07)
