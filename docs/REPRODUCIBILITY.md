# Reproducibility note for reviewers

**Updated:** 2026-05-16 (Phase 5 Group B)
**Audience:** Academic reviewers auditing the implementation against the research paper "How to Quantify Stock Sentiment".

This repository implements a live multi-source sentiment scoring pipeline. The implementation is real software running in production on Railway; it is not a static snapshot. As a result there is a hard split between *what a reviewer can verify offline by cloning this repo* and *what they cannot reproduce without the production infrastructure*. This note tells you which is which, so you can audit the parts that are reproducible and trust the parts that aren't with appropriate skepticism.

---

## What you can reproduce locally

### The scoring math

Every formula the paper describes — z-score normalization, the per-signal weight `w_i = w_src · w_rel · w_conf · w_author · e^(−λΔt)`, FinBERT class-probability entropy weighting, sub-index aggregation (volume-weighted average + shrinkage for narrative/influencer; 6-component for market; paper-direct weighted average without shrinkage for macro), composite weighting, missing-layer redistribution, EMA smoothing, semantic dedup clustering — is implemented in pure Python with no live-infrastructure dependency. All of it is exercised by the unit-test suite.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest                  # ~31 unit-test files, all green; ~8 s on a modern laptop
```

`conftest.py` auto-deselects tests marked `@pytest.mark.integration`. The deselected files (`integration_test_connections.py`, `integration_test_sources.py`) are the only places that need a live database, Redis, or external API; everything else runs purely from in-memory test fixtures. The result of a clean run is the canonical evidence that the scoring math behaves as the paper specifies.

To run integration tests explicitly (only if you have set up the infrastructure):

```bash
pytest -m integration
```

### The schema

The full PostgreSQL schema lives in `migrations.sql` (base) and `migrations/005_*.sql` … `008_*.sql` (additive). [`docs/DATA_DICTIONARY.md`](DATA_DICTIONARY.md) walks through every column and ties it to the methodology. You do not need to actually apply the schema to inspect it.

### The methodology audits

The repository ships four post-Phase-1 audits in `docs/audit_*_postphase1.md`. Each audit re-checks the deployed code against a list of paper-derived requirements ("does the system implement z-score normalization?", "is the composite weight 0.35 for market?", etc.) and records the resolution per gap. Audit D was refreshed for the Phase-4 macro changes; audits A–C are pre-Phase-3/4. See the [Audits & history](#audits--history) section below for the reading order.

### The code

All paper-mentioned modules are tracked in this repo:

- `pipeline/features/normalize.py` — the per-signal weight formula and the z-score normalizer.
- `pipeline/scoring/subindices.py` — the generic, market-specific, and macro-specific aggregators.
- `pipeline/scoring/composite.py` — the channel weights and missing-layer redistribution.
- `pipeline/scoring/ema.py` — the variable-timestep 4-hour-half-life EMA.
- `pipeline/nlp/finbert.py` — FinBERT inference, batch chunking, `inference_mode`.
- `pipeline/nlp/dedup.py` — semantic clustering (4-hour window, cosine > 0.85, `all-MiniLM-L6-v2`).
- `pipeline/sources/*.py` — per-channel ingesters (`market`, `narrative`, `influencer`, `macro`, `short_volume`, `fred`).

You can read these files in isolation. The README links each formula to the file where it lives.

---

## What you cannot reproduce locally

### The live pipeline

The pipeline ingests live market data, news articles, insider filings, and macroeconomic series from five third-party APIs (Alpha Vantage, Finnhub, FRED, FINRA REGSHO file feed, yfinance), persists them to a PostgreSQL database, and serves cached results from Redis. None of those components are mocked or stubbed in a way that runs locally without keys. Specifically:

- **No SQLite or DuckDB fallback.** All queries are written against asyncpg with PostgreSQL-specific features (`ON CONFLICT`, `DISTINCT ON`, `JSONB`). `db/connection.py` reads `os.environ["DATABASE_URL"]` and raises `KeyError` if unset.
- **No in-memory Redis.** `db/redis.py` reads `os.environ["REDIS_URL"]`.
- **No frozen-time replay.** Backfill scripts under `backfill/` exist to seed historical data when first wiring up an environment, but they themselves call live API endpoints and require keys.
- **No public anonymized data dump.** The production database is on Railway; we do not currently publish a redacted snapshot. Reviewers who need to verify outputs against a sample of real data should request a snapshot directly.

### Live scores

A reviewer cannot, by cloning this repo, produce a composite score for `AAPL` matching the score the live API would return at the same time. The composite is the output of a continuous process: 30-minute scoring ticks read from a `raw_signals` table whose history goes back to April 2024, apply rolling z-scores over hundreds of past observations per signal, and blend EMA-smoothed composites against the previous tick. None of that state is checked in; recreating it would require running the pipeline against the same upstream APIs for an extended period.

### The FinBERT model

`pipeline/nlp/finbert.py` lazy-loads `ProsusAI/finbert` (~440 MB) from Hugging Face on first call. Pulling the model requires network access. Once cached locally, FinBERT *can* be exercised by writing a small driver script — but no unit test depends on having the model available, because tests stub the scoring function.

---

## Required environment variables

A canonical `.env.example` lives at the repository root. Reviewers who want to actually run the live pipeline must populate at minimum:

| Variable | Why required | Where it's read |
|---|---|---|
| `DATABASE_URL` | Postgres pool | `db/connection.py` |
| `REDIS_URL` | Current-state cache + rate limiter | `db/redis.py` |
| `ALPHA_VANTAGE_KEY` | Narrative news + relevance scores | `pipeline/sources/narrative.py`, `pipeline/sources/macro.py`, `backfill/{ohlcv,etf}_backfill.py` |
| `FINNHUB_KEY` | Narrative fallback + insider + analyst | `pipeline/sources/{narrative,influencer,macro}.py` |
| `FRED_API_KEY` | Treasury yields + TED-substitute | `pipeline/sources/fred.py` |

`NEWSAPI_KEY` and `OPENAI_API_KEY` are listed in `.env.example` for completeness but are not read by any current code path (NewsAPI was excluded from the implemented methodology; the OpenAI-backed Pro-tier explanation is planned but not deployed — today both tiers use the template-based explainer).

For the API itself you do not need to set keys via environment variables: API tokens are minted by `tools/generate_keys.py`, stored as SHA-256 hashes in the `api_keys` table, and looked up by `api/auth.py` on every request. The plaintext is printed once at generation time and never recoverable from the database.

---

## Audits & history

The reading order I would recommend for a paper reviewer:

1. **[`README.md`](../README.md)** — top-down system description with current weights and links to the modules that implement each formula.
2. **This file (`REPRODUCIBILITY.md`)** — what you can and cannot run.
3. **[`docs/DATA_DICTIONARY.md`](DATA_DICTIONARY.md)** — every column in every table, tied back to the methodology.
4. **[`docs/audit_D_influencer_macro_postphase1.md`](audit_D_influencer_macro_postphase1.md)** — most current of the four audits; covers through P4.4.
5. **[`docs/audit_C_composite_postphase1.md`](audit_C_composite_postphase1.md)** — composite construction, divergence cap, EMA smoothing.
6. **[`docs/audit_A_market_postphase1.md`](audit_A_market_postphase1.md)** — market sub-index. Dated 2026-05-10 (pre-P3/P4) but the market layer has not changed materially since.
7. **[`docs/audit_B_narrative_postphase1.md`](audit_B_narrative_postphase1.md)** — narrative; preserved as a record of the pre-Sprint-A gap inventory. Note that Sprint A (FinBERT integration, w_conf, source-weight reconciliation) and Sprint D (relevance 0.60) both shipped after this audit was written — see [`CHANGELOG.md`](../CHANGELOG.md) and the live `_SOURCE_WEIGHTS` / `_compute_w_conf` definitions in `normalize.py` for the current state.
8. **[`docs/diagnostic_github_2026-05-16.md`](diagnostic_github_2026-05-16.md)** — the Phase 5 cleanup diagnostic that triggered this round of documentation work. Useful as a meta-view of where the repo stood before the Phase-5 cleanup.

The audits use a four-state vocabulary: **RESOLVED** (deployed and verified), **ACCEPTED** (intentional deviation from the paper, documented), **OPEN** (gap remains; tracked), **REGRESSION** (was working, broke later). Reviewers should pay attention to ACCEPTED items in particular — those are the places where the deployed system deliberately differs from the paper text, usually because the code is more correct or more current and the paper text is queued for a future edit cycle. See for example the TED-substitute discussion in `pipeline/sources/fred.py`.

---

## Things the paper claims that the code does not implement

For transparency, these are the methodology elements the paper describes that have **not** landed in the deployed code yet, listed so a reviewer doesn't waste time hunting for them:

- **SEC EDGAR 8-K narrative source.** Sprint C drafted but never merged. Retracted in Phase 5 (2026-05-16) and moved to Future Additions. The source label `sec_edgar` has been removed from `_SOURCE_WEIGHTS`. The paper text needs to be reconciled in the next paper-edit cycle.
- **ApeWisdom retail-sentiment ingester.** Moved to Future Additions per the Sprint E paper-vs-code reconciliation (2026-05-12).
- **Role-based `w_author` hierarchy.** The paper explicitly defers this. Today `w_author = 1.00` uniformly. The scaffold is in place (`_INFLUENCER_W_AUTHOR` in `normalize.py`).
- **GPT-4o-mini Pro-tier explanations.** Planned. Today both tiers use the template-based explainer in `pipeline/explanation/templates.py`.
- **`backtest_results` forward-return population.** The table exists and is queryable; the asynchronous backfill that fills `forward_return_*` columns has not been built.

The audit files track several smaller gaps under "OPEN" and "ACCEPTED" — see audits A–D for the line-item inventory.
