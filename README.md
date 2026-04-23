
---

# SentientMarkets Sentiment API — v1 Technical Spec (Final)

---

## What It Is

A public API that accepts a stock ticker and returns a structured sentiment score built from multiple data sources. One input, one clean JSON output. The score reflects what the market is doing, what people are saying, what analysts think, and what insiders are committing to — combined into a single defensible number with a full breakdown behind it.

---

## The Research Foundation

**Treat sentiment as four separate channels, not one.** Market actions, public narrative, analyst opinion, and insider behaviour are fundamentally different signal types and must be scored separately before combining.

**Source hierarchy matters.** Official market data and filings are most trustworthy. Licensed news and analyst data sit below that. Social and retail chatter sits at the bottom — useful but noisy, manipulation-prone, and treated as opportunistic not foundational.

**Precompute, don't compute on demand.** A system that runs full multi-source inference on every API request becomes a bottleneck machine. The right architecture keeps sentiment state continuously updated in the background and serves cached results at request time.

**Validation is non-negotiable.** Signal quality must be tested against actual price outcomes, not just text benchmarks. Do this early — even simple checks like top 20% sentiment vs average forward return reveal whether each sub-index is adding real signal.

---

## Production Architecture

### The Two-Track Design

**Background Pipeline — System A**
Runs continuously on a schedule. Never triggered by a user request. All computation, math, and scoring happens here.

```
Layer 02 — Orchestration
Layer 03 — Market Data
Layer 04 — Influencer Activity
Layer 05 — Narrative Sentiment
Layer 06 — Macro Context
Layer 07 — Feature Engineering / Normalization
Layer 08 — Scoring / Composite Engine
Layer 09 — Confidence / Quality
Layer 10 — Explanation / Interpretation
           ↓ writes to Redis (current state)
           ↓ writes to PostgreSQL (permanent record)
```

**Request-Serving Path — System B**
Runs on every API request. Contains no computation. Reads only.

```
Layer 01 — Request / API Gateway
Layer 11 — Response Assembly
           ↑ reads from Redis
```

---

### The Three Systems

**System A — Ingestion & Scoring Engine**
The brain. All math, all NLP, all scoring happens here. Runs on APScheduler. Four scheduled processes:
- Market data every 15 minutes (market hours, weekdays only)
- Narrative sentiment every 30 minutes
- Influencer activity every 6 hours
- Macro context daily at 2am

Scores all tickers in the active universe on each cycle. Writes current state to Redis and appends immutable records to PostgreSQL.

**System B — API**
The mouth. Thin, read-only. Target response time under 100ms for cached results. Handles auth, tier routing, and JSON assembly only.

Redis behaviour: always serve the last known state. Never expire keys aggressively. Rely on freshness timestamps and confidence flags to communicate data age to the caller. A low-confidence response is more useful than a key-not-found error.

**System C — Signal Logger**
The memory. Append-only PostgreSQL tables. Every completed scoring cycle writes finished outputs here permanently. Enables the historical sentiment endpoint as a paid API feature and offline backtest analysis.

---

## Active Ticker Universe

System A only scores tickers in the defined active universe. This prevents unbounded compute costs. The universe is defined as:

- **Tier 1 — Supported universe:** manually curated list of the top 500 most liquid US equities. Always scored on every cycle.
- **Tier 2 — User-requested:** any ticker requested via API in the last 7 days is added to the active scoring queue automatically.
- **Out of universe:** tickers with insufficient market history or liquidity return a structured no-data response rather than a score.

This must be enforced in Layer 02 before any data fetching begins.

---

## Data Source Contract Per Layer

### Layer 03 — Market Data

| Signal | Primary Source | Fallback | Notes |
|---|---|---|---|
| Price / OHLCV | Alpha Vantage | Polygon | yfinance for backfill and development only |
| RSI / technicals | Alpha Vantage | Finnhub | Pre-computed indicators endpoint |
| Put/call ratio | Finnhub | Polygon | Options availability varies by tier |
| Short interest | FINRA via Finnhub | — | Updates twice monthly — staleness flagged |
| Implied volatility | Finnhub | Alpha Vantage | |

### Layer 04 — Influencer Activity

| Signal | Primary Source | Notes |
|---|---|---|
| Insider transactions | SEC EDGAR Forms 3/4/5 | Form 4 due within 2 business days. Use commercial intermediary (Finnhub, Polygon) for near-real-time — the quarterly bulk dataset is not a live feed |
| Analyst upgrades/downgrades | Finnhub | Rating change events with timestamps |
| Analyst target price changes | Finnhub | |

### Layer 05 — Narrative Sentiment

| Signal | Primary Source | Notes |
|---|---|---|
| News + metadata | Alpha Vantage NEWS_SENTIMENT | Returns provider sentiment, title, summary, source, relevance score |
| Additional news | NewsAPI, Finnhub news | Stored in raw_articles for Phase 2 FinBERT scoring |
| Social mentions | ApeWisdom | Opportunistic — failure degrades confidence, does not break layer |

Phase 1: use Alpha Vantage provider sentiment scores. Store all raw article objects in raw_articles table for Phase 2.

### Layer 06 — Macro Context

| Signal | Primary Source | Notes |
|---|---|---|
| VIX | Alpha Vantage / Finnhub | |
| Sector ETF trend | Alpha Vantage | 20-day return on relevant sector ETF |
| Broad risk proxy | VIX term structure or SPY momentum | |

CNN Fear & Greed removed as a named dependency — no official stable API exists.

---

## The Eleven Layers

**Layer 01 — Request/API Gateway**
Validates ticker against supported universe. Authenticates API key. Enforces rate limits. Routes to correct tier. Returns structured no-data response for unsupported or unscored tickers.

**Layer 02 — Orchestration**
Runs Layers 03–06 in parallel via asyncio.gather(). Explicit fallback propagation rule per layer:
```
If primary source fails   → try fallback source
If fallback fails         → use last known value IF within staleness threshold
If stale beyond threshold → mark layer missing, remove from composite,
                            redistribute weights, apply confidence penalty
```

**Layer 03 — Market Data**
Measures what money is doing. Produces Market Sub-Index.

**Layer 04 — Influencer Activity**
Measures what influential actors are doing. Produces Influencer Sub-Index.

**Layer 05 — Narrative Sentiment**
Measures what the public information environment is saying. Produces Narrative Sub-Index.

Deduplication rule: cluster articles within a 2–4 hour time window. If cosine similarity > 0.85, treat as the same event — keep the highest credibility source and merge weights. Otherwise treat as independent signals.

**Layer 06 — Macro Context**
Measures whether the broader environment helps or hurts. Produces Macro Sub-Index. Acts as a modifier with conservative weight (10%).

**Layer 07 — Feature Engineering/Normalization**
Shared utility called by all four data layers. Z-scoring, scaling, time decay, source weighting, confidence weighting.

**Layer 08 — Scoring/Composite Engine**
Combines four sub-indices into composite score. Handles missing layer weight redistribution, divergence detection, macro caps. Produces ranked structured drivers using this explicit schema:

```json
{
  "signal": "Insider purchase",
  "description": "CEO bought 12,000 shares",
  "direction": "bullish",
  "magnitude": 0.8,
  "source_layer": "influencer",
  "confidence": 0.9
}
```

Extreme imbalance guardrail: if one layer scores above 85 and another scores below 30, cap the composite at 75 until the conflict resolves.

**Layer 09 — Confidence/Quality**
Penalty system. Starts at 100. Subtracts penalties. Clips to [0, 100]. No bonuses. Integer output.

Staleness thresholds:

| Layer | Signal | Max Age Before Stale |
|---|---|---|
| Market | Price / volume | 90 minutes |
| Narrative | News | 6–12 hours |
| Influencer | Analyst ratings | 3–5 days |
| Influencer | Insider filings | 30–45 days |
| Macro | VIX / sector ETF | 1 day |

Penalty deductions:
- Missing layer: −15 each
- Stale data: −10 per source
- Low signal volume (fewer than 5 signals): −20
- High divergence (spread > 40 points): −15
- Insufficient history (cross-sectional fallback active): −5 per affected signal

**Layer 10 — Explanation/Interpretation**
Receives ranked structured drivers from Layer 08. Verbalizes them into text. Cannot invent signals not present in the driver list. Free tier: templates. Pro tier: GPT-4o-mini grounded strictly on the driver schema.

**Layer 11 — Response Assembly**
Reads Redis. Builds final JSON. Applies tier filtering.

---

## Label Mapping

Composite score maps deterministically to a sentiment label:

| Score | Label |
|---|---|
| 0–20 | Strongly Bearish |
| 21–40 | Bearish |
| 41–60 | Neutral |
| 61–80 | Bullish |
| 81–100 | Strongly Bullish |

---

## The Mathematics

**Z-scoring** applies to individual raw signals within each data layer only — never to sub-indices.

```
z = (x − μ) / σ
```

Fallback when history is insufficient:
- 30–89 days: use available history, apply −5 confidence penalty per affected signal
- 10–29 days: cross-sectional normalization against peer universe (same GICS sector + market-cap bucket), apply −5 confidence penalty per affected signal
- Under 10 days: exclude signal entirely, treat as missing

**Peer universe** for cross-sectional normalization: same GICS sector and market-cap bucket (large/mid/small cap). This must be consistent across all engineers — do not deviate.

**Scaling** maps z-scores to 0–100 where 50 is always neutral. Bearish-direction signals are inverted before scaling.

```
score = (clip(z, −3, +3) + 3) / 6 × 100
```

**Time decay:**

```
w_time = e^(−λ × age_hours)     λ = ln(2) / half_life

Half-lives:
  social:   4 hours
  news:     24 hours
  analyst:  168 hours (1 week)
  insider:  720 hours (30 days)
  market:   1 hour
```

**Combined weighting:**

```
w = w_source × w_confidence × w_time

Source weights:
  SEC filings / market data:  0.95–1.00
  Licensed news (AV):         0.85
  Finnhub news:               0.75
  NewsAPI:                    0.65
  Reddit / social:            0.40
```

**Sub-index aggregation** with volume shrinkage:

```
Sub-Index = Σ(w_i × score_i) / Σ(w_i)
score = 50 + min(1.0, n_signals / 5) × (raw_score − 50)
```

**Composite scoring:**

```
Composite = 0.35 × Market
          + 0.30 × Narrative
          + 0.25 × Influencer
          + 0.10 × Macro
```

Missing layers have their weight redistributed proportionally across remaining layers.

**Divergence detection:** spread above 40 points flagged as high divergence. If one layer exceeds 85 and another is below 30, composite is capped at 75.

---

## The Database Structure

One PostgreSQL database, three table groups with clearly separated roles.

### System A's Working Tables (internal only, never API-facing)

```sql
-- Numeric signals for z-score computation
CREATE TABLE raw_signals (
    id           SERIAL PRIMARY KEY,
    ticker       VARCHAR(10),
    signal_type  VARCHAR(50),
    value        FLOAT,
    source       VARCHAR(50),
    upload_type  VARCHAR(20),   -- 'manual_backfill' or 'live'
    timestamp    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_raw_signals_lookup
ON raw_signals (ticker, signal_type, timestamp DESC);

-- Raw article objects for narrative layer
-- Separate from raw_signals because text objects are not numeric
-- Required for deduplication, FinBERT (Phase 2), and aspect extraction
CREATE TABLE raw_articles (
    id                  SERIAL PRIMARY KEY,
    ticker              VARCHAR(10),
    title               TEXT,
    summary             TEXT,
    source              VARCHAR(50),
    source_url          TEXT,
    published_at        TIMESTAMPTZ,
    provider_sentiment  FLOAT,
    relevance_score     FLOAT,
    event_cluster_id    VARCHAR(100),   -- set after deduplication
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_raw_articles_lookup
ON raw_articles (ticker, published_at DESC);
```

### System C's Output Tables (append-only, API-facing)

```sql
-- One row per ticker per scoring cycle — never updated
CREATE TABLE sentiment_history (
    id                SERIAL PRIMARY KEY,
    ticker            VARCHAR(10),
    composite_score   FLOAT,
    market_index      FLOAT,
    narrative_index   FLOAT,
    influencer_index  FLOAT,
    macro_index       FLOAT,
    confidence_score  INTEGER,          -- 0–100, integer, no bonuses
    confidence_flags  JSONB,
    top_drivers       JSONB,
    divergence        VARCHAR(20),
    market_as_of      TIMESTAMPTZ,
    narrative_as_of   TIMESTAMPTZ,
    influencer_as_of  TIMESTAMPTZ,
    macro_as_of       TIMESTAMPTZ,
    timestamp         TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Price at moment of scoring for backtest return calculation
CREATE TABLE price_snapshots (
    id         SERIAL PRIMARY KEY,
    ticker     VARCHAR(10),
    close      FLOAT,
    volume     BIGINT,
    timestamp  TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Precomputed forward returns — avoids heavy joins during analysis
CREATE TABLE backtest_results (
    id                 SERIAL PRIMARY KEY,
    ticker             VARCHAR(10),
    score_timestamp    TIMESTAMPTZ,
    composite_score    FLOAT,
    forward_return_1d  FLOAT,
    forward_return_5d  FLOAT,
    forward_return_20d FLOAT,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
```

forward_return columns are populated asynchronously once sufficient time has elapsed after the score timestamp.

### Redis (current state layer)

```
Key:   sentiment:{ticker}
Value: latest scored JSON object
TTL:   long — do not expire aggressively
```

Always serve last known state. Let freshness and confidence flags communicate quality.

---

## Response Schema

### No-Data Response (unsupported, unscored, or insufficient history)

```json
{
  "ticker": "XYZ",
  "status": "insufficient_data",
  "message": "Not enough historical data to compute a reliable sentiment score yet."
}
```

Other status values: `unsupported_ticker`, `temporarily_unavailable`.

### Free Tier

```json
{
  "ticker": "AAPL",
  "score": 72,
  "label": "Bullish",
  "confidence": 81,
  "timestamp": "2026-04-23T14:32:00Z"
}
```

### Pro Tier

```json
{
  "ticker": "AAPL",
  "score": 72,
  "label": "Bullish",
  "confidence": 81,
  "sub_indices": {
    "market": 78,
    "narrative": 69,
    "influencer": 80,
    "macro": 61
  },
  "divergence": "aligned",
  "aspects": {
    "revenue_growth": "positive",
    "margin_pressure": "neutral",
    "guidance": "positive"
  },
  "top_drivers": [
    {
      "signal": "Insider purchase",
      "description": "CEO bought 12,000 shares",
      "direction": "bullish",
      "magnitude": 0.8,
      "source_layer": "influencer",
      "confidence": 0.9
    },
    {
      "signal": "Earnings beat",
      "description": "Beat estimates by 8%",
      "direction": "bullish",
      "magnitude": 0.76,
      "source_layer": "narrative",
      "confidence": 0.85
    },
    {
      "signal": "RSI elevated",
      "description": "RSI at 71, overbought territory",
      "direction": "bearish",
      "magnitude": 0.45,
      "source_layer": "market",
      "confidence": 0.78
    }
  ],
  "explanation": "Sentiment is primarily driven by strong insider conviction and a recent earnings beat. Near-term technical conditions show mild overbought pressure.",
  "freshness": {
    "market_as_of":     "2026-04-23T14:30:00Z",
    "narrative_as_of":  "2026-04-23T14:00:00Z",
    "influencer_as_of": "2026-04-23T08:00:00Z",
    "macro_as_of":      "2026-04-23T02:00:00Z"
  },
  "confidence_flags": [],
  "cache_age_seconds": 480
}
```

---

## The Technology

| Component | Technology |
|---|---|
| API framework | FastAPI |
| Background scheduling | APScheduler |
| Current state cache | Redis |
| Persistent database | PostgreSQL |
| Phase 1 NLP | Alpha Vantage NEWS_SENTIMENT provider scores |
| Phase 2 NLP | FinBERT — article-level scoring, not sentence-level |
| Lexicon baseline | Loughran-McDonald finance dictionary |
| Deduplication | Hash matching + cosine similarity on sentence embeddings |
| Pro explanations | GPT-4o-mini grounded on structured driver schema only |
| Market data | Alpha Vantage, Finnhub, Polygon |
| Development / backfill only | yfinance |
| News | Alpha Vantage NEWS_SENTIMENT, NewsAPI, Finnhub news |
| Social (opportunistic) | ApeWisdom |
| Insider data | SEC EDGAR direct filing parse or Finnhub/Polygon intermediary |
| Macro | VIX, sector ETFs, broad risk proxy |
| Deployment | Railway (Systems A, B, C + PostgreSQL + Redis) |

---

## The Build Phases

**Phase 1 — Reliable and fast**
Manual backfill of raw_signals and raw_articles before launch. Active ticker universe defined and enforced. Market layer fully operational. Narrative layer using Alpha Vantage provider sentiment scores. Influencer layer with insider trades and analyst changes. Thin macro layer (VIX + sector ETF only). Confidence flags with explicit staleness thresholds. Per-layer freshness timestamps in every response. Redis always-serve-last-known-state behaviour. Template-based explanations grounded on driver schema. Fallback propagation rules implemented per layer. No-data response shapes defined and handled.

**Phase 2 — Smarter signals**
FinBERT running in background pipeline only, never in request path. Article-level scoring using stored raw_articles. Proper similarity-based deduplication with 2–4 hour clustering windows. Aspect scoring on filings and earnings calls. Divergence logic and extreme imbalance caps in composite engine. GPT-4o-mini explanations for Pro tier. Validation of each sub-index against forward returns from backtest_results before expanding scope.

**Phase 3 — Premium differentiation**
Earnings transcript analysis. Management tone scoring. Backtested weight calibration. Historical sentiment endpoint as paid feature. Actor quality weighting for analyst signals. Richer macro modelling.

---

## Execution Risks

**Signal quality** is the highest risk. Validate each sub-index against forward returns after Phase 1. Cut or downweight any layer not adding real signal.

**Fallback discipline** must be built in Phase 1. Every source needs a defined primary, fallback, staleness threshold, and confidence penalty before the system goes live.

**Active universe scope** must be enforced from day one. Without it, System A quietly becomes expensive and unpredictable.

**FinBERT** belongs in the background pipeline only. Article-level in Phase 2. Never in the request path.

**Explanation grounding** is non-negotiable. Layer 10 receives the driver schema from Layer 08 and verbalizes it. It does not receive the raw score and invent reasoning.

**raw_articles is required** for Phase 2 to work. Store article objects from day one even if Phase 1 only uses provider sentiment scores. Without stored text, FinBERT and aspect extraction have no inputs to work from.

---

# Artifact 1 — API Contract

## Base URL
```
https://api.sentientmarkets.com/v1
```

## Authentication
All requests require a Bearer token in the Authorization header.
```
Authorization: Bearer sk-sm-xxxxxxxxxxxx
```

---

## Endpoints

### GET /v1/sentiment/{ticker}

Returns the latest pre-computed sentiment score for a ticker.

**Path parameters**

| Parameter | Type | Required | Description |
|---|---|---|---|
| ticker | string | yes | US-listed equity ticker symbol e.g. AAPL |

**Query parameters**

| Parameter | Type | Required | Description |
|---|---|---|---|
| detail | string | no | `summary` (default) or `full`. Full only available on Pro tier |
| refresh | boolean | no | If true, queues a background refresh. Returns current cached score immediately |

**Response — 200 OK (free tier)**
```json
{
  "ticker": "AAPL",
  "score": 72,
  "label": "Bullish",
  "confidence": 81,
  "timestamp": "2026-04-23T14:32:00Z",
  "cache_age_seconds": 480
}
```

**Response — 200 OK (Pro tier, detail=full)**
```json
{
  "ticker": "AAPL",
  "score": 72,
  "label": "Bullish",
  "confidence": 81,
  "sub_indices": {
    "market": 78,
    "narrative": 69,
    "influencer": 80,
    "macro": 61
  },
  "divergence": "aligned",
  "aspects": {
    "revenue_growth": "positive",
    "margin_pressure": "neutral",
    "guidance": "positive"
  },
  "top_drivers": [
    {
      "signal": "Insider purchase",
      "description": "CEO bought 12,000 shares",
      "direction": "bullish",
      "magnitude": 0.8,
      "source_layer": "influencer",
      "confidence": 0.9
    },
    {
      "signal": "Earnings beat",
      "description": "Beat estimates by 8%",
      "direction": "bullish",
      "magnitude": 0.76,
      "source_layer": "narrative",
      "confidence": 0.85
    },
    {
      "signal": "RSI elevated",
      "description": "RSI at 71, overbought territory",
      "direction": "bearish",
      "magnitude": 0.45,
      "source_layer": "market",
      "confidence": 0.78
    }
  ],
  "explanation": "Sentiment is primarily driven by strong insider conviction and a recent earnings beat. Near-term technical conditions show mild overbought pressure.",
  "freshness": {
    "market_as_of":     "2026-04-23T14:30:00Z",
    "narrative_as_of":  "2026-04-23T14:00:00Z",
    "influencer_as_of": "2026-04-23T08:00:00Z",
    "macro_as_of":      "2026-04-23T02:00:00Z"
  },
  "confidence_flags": [],
  "cache_age_seconds": 480
}
```

**Response — 200 OK (no data)**
```json
{
  "ticker": "XYZ",
  "status": "insufficient_data",
  "message": "Not enough historical data to compute a reliable sentiment score yet."
}
```

Status values: `insufficient_data`, `unsupported_ticker`, `temporarily_unavailable`.

**Error responses**

| Status | Code | Description |
|---|---|---|
| 401 | unauthorized | Invalid or missing API key |
| 429 | rate_limit_exceeded | Too many requests for your tier |
| 404 | ticker_not_found | Ticker does not exist |
| 500 | internal_error | Unexpected server error |

---

### GET /v1/sentiment/{ticker}/history

Returns historical sentiment scores for a ticker. Pro tier only.

**Query parameters**

| Parameter | Type | Required | Description |
|---|---|---|---|
| days | integer | no | Lookback window in days. Default 30, max 365 |
| interval | string | no | `daily` (default) or `raw` (every scoring cycle) |

**Response — 200 OK**
```json
{
  "ticker": "AAPL",
  "history": [
    {
      "timestamp": "2026-04-22T14:30:00Z",
      "score": 68,
      "label": "Bullish",
      "confidence": 79,
      "sub_indices": {
        "market": 71,
        "narrative": 65,
        "influencer": 74,
        "macro": 58
      }
    }
  ]
}
```

---

### GET /v1/tickers

Returns the list of tickers in the supported universe.

**Response — 200 OK**
```json
{
  "universe_size": 500,
  "tickers": ["AAPL", "MSFT", "NVDA", "..."]
}
```

---

### GET /v1/status

Returns API health and system freshness.

**Response — 200 OK**
```json
{
  "status": "operational",
  "last_market_run":     "2026-04-23T14:30:00Z",
  "last_narrative_run":  "2026-04-23T14:00:00Z",
  "last_influencer_run": "2026-04-23T08:00:00Z",
  "last_macro_run":      "2026-04-23T02:00:00Z"
}
```

---

## Label Mapping

| Score | Label |
|---|---|
| 0–20 | Strongly Bearish |
| 21–40 | Bearish |
| 41–60 | Neutral |
| 61–80 | Bullish |
| 81–100 | Strongly Bullish |

## Rate Limits

| Tier | Requests/min | Endpoints |
|---|---|---|
| Free | 10 | /sentiment/{ticker} summary only |
| Pro | 120 | All endpoints including history and full detail |

---

---

# Artifact 2 — SQL Schema / Migrations

```sql
-- ============================================================
-- Migration 001 — System A working tables
-- ============================================================

CREATE TABLE raw_signals (
    id           SERIAL PRIMARY KEY,
    ticker       VARCHAR(10)  NOT NULL,
    signal_type  VARCHAR(50)  NOT NULL,
    value        FLOAT        NOT NULL,
    source       VARCHAR(50)  NOT NULL,
    upload_type  VARCHAR(20)  NOT NULL CHECK (upload_type IN ('manual_backfill', 'live')),
    timestamp    TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_raw_signals_lookup
ON raw_signals (ticker, signal_type, timestamp DESC);

CREATE INDEX idx_raw_signals_ticker_ts
ON raw_signals (ticker, timestamp DESC);


-- ============================================================
-- Migration 002 — Narrative article storage
-- ============================================================

CREATE TABLE raw_articles (
    id                  SERIAL PRIMARY KEY,
    ticker              VARCHAR(10)  NOT NULL,
    title               TEXT         NOT NULL,
    summary             TEXT,
    source              VARCHAR(50)  NOT NULL,
    source_url          TEXT,
    published_at        TIMESTAMPTZ  NOT NULL,
    provider_sentiment  FLOAT,
    relevance_score     FLOAT,
    content_hash        VARCHAR(64),              -- MD5 hash for exact dedup
    event_cluster_id    VARCHAR(100),             -- set after similarity clustering
    finbert_score       FLOAT,                    -- populated in Phase 2
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_raw_articles_ticker_ts
ON raw_articles (ticker, published_at DESC);

CREATE INDEX idx_raw_articles_cluster
ON raw_articles (ticker, event_cluster_id);

CREATE UNIQUE INDEX idx_raw_articles_hash
ON raw_articles (ticker, content_hash);


-- ============================================================
-- Migration 003 — System C output tables
-- ============================================================

CREATE TABLE sentiment_history (
    id                SERIAL PRIMARY KEY,
    ticker            VARCHAR(10)  NOT NULL,
    composite_score   FLOAT        NOT NULL,
    market_index      FLOAT,
    narrative_index   FLOAT,
    influencer_index  FLOAT,
    macro_index       FLOAT,
    confidence_score  SMALLINT     NOT NULL CHECK (confidence_score BETWEEN 0 AND 100),
    confidence_flags  JSONB,
    top_drivers       JSONB,
    divergence        VARCHAR(20),
    market_as_of      TIMESTAMPTZ,
    narrative_as_of   TIMESTAMPTZ,
    influencer_as_of  TIMESTAMPTZ,
    macro_as_of       TIMESTAMPTZ,
    timestamp         TIMESTAMPTZ  NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sentiment_history_ticker_ts
ON sentiment_history (ticker, timestamp DESC);


CREATE TABLE price_snapshots (
    id         SERIAL PRIMARY KEY,
    ticker     VARCHAR(10)  NOT NULL,
    close      FLOAT        NOT NULL,
    volume     BIGINT,
    timestamp  TIMESTAMPTZ  NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_price_snapshots_ticker_ts
ON price_snapshots (ticker, timestamp DESC);


CREATE TABLE backtest_results (
    id                  SERIAL PRIMARY KEY,
    ticker              VARCHAR(10)  NOT NULL,
    score_timestamp     TIMESTAMPTZ  NOT NULL,
    composite_score     FLOAT        NOT NULL,
    forward_return_1d   FLOAT,
    forward_return_5d   FLOAT,
    forward_return_20d  FLOAT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_backtest_ticker_ts
ON backtest_results (ticker, score_timestamp DESC);


-- ============================================================
-- Migration 004 — API management tables
-- ============================================================

CREATE TABLE api_keys (
    id           SERIAL PRIMARY KEY,
    key_hash     VARCHAR(128) NOT NULL UNIQUE,   -- store hash, never plaintext
    tier         VARCHAR(20)  NOT NULL CHECK (tier IN ('free', 'pro')),
    owner_email  VARCHAR(255),
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE ticker_universe (
    id           SERIAL PRIMARY KEY,
    ticker       VARCHAR(10)  NOT NULL UNIQUE,
    tier         VARCHAR(20)  NOT NULL CHECK (tier IN ('tier1_supported', 'tier2_requested')),
    added_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_requested_at TIMESTAMPTZ
);

CREATE INDEX idx_ticker_universe_tier
ON ticker_universe (tier);
```

---

---

# Artifact 3 — Service / Module Layout

```
sentimentapi/
│
├── main.py                         # FastAPI app entrypoint
├── requirements.txt
├── .env.example
├── Dockerfile
├── railway.toml
│
├── api/                            # System B — request-serving path only
│   ├── __init__.py
│   ├── auth.py                     # API key validation, tier lookup
│   ├── rate_limit.py               # Per-tier rate limiting middleware
│   ├── routes/
│   │   ├── sentiment.py            # GET /v1/sentiment/{ticker}
│   │   ├── history.py              # GET /v1/sentiment/{ticker}/history
│   │   ├── tickers.py              # GET /v1/tickers
│   │   └── status.py               # GET /v1/status
│   └── response/
│       ├── assembler.py            # Reads Redis, builds JSON response
│       ├── labels.py               # Score → label mapping (0-20 → Strongly Bearish etc)
│       └── schemas.py              # Pydantic response models
│
├── pipeline/                       # System A — background scoring engine
│   ├── __init__.py
│   ├── scheduler.py                # APScheduler setup, job registration
│   ├── orchestrator.py             # asyncio.gather, fallback propagation, timeout logic
│   │
│   ├── sources/                    # Layer 03–06 data fetchers
│   │   ├── market.py               # Alpha Vantage, Finnhub — price, RSI, IV, PCR, short interest
│   │   ├── influencer.py           # SEC EDGAR Forms 3/4/5, Finnhub analyst ratings
│   │   ├── narrative.py            # Alpha Vantage NEWS_SENTIMENT, NewsAPI, ApeWisdom
│   │   └── macro.py                # VIX, sector ETF trend, risk proxy
│   │
│   ├── nlp/                        # Narrative processing
│   │   ├── dedup.py                # Hash dedup + cosine similarity clustering
│   │   ├── entity_linker.py        # Map article text to confirmed ticker
│   │   ├── relevance.py            # Filter irrelevant articles
│   │   └── finbert.py              # Phase 2 — FinBERT inference wrapper (article-level)
│   │
│   ├── features/                   # Layer 07 — normalization utilities
│   │   ├── zscore.py               # Z-score with 90-day history lookup + fallbacks
│   │   ├── scaling.py              # Clip and scale to 0–100
│   │   ├── decay.py                # Time decay weights per source half-life
│   │   ├── weighting.py            # Source weight × confidence × decay combiner
│   │   └── shrinkage.py            # Volume shrinkage toward 50
│   │
│   ├── scoring/                    # Layer 08 — composite engine
│   │   ├── subindices.py           # Per-layer weighted aggregation
│   │   ├── composite.py            # Weighted combination, missing layer redistribution
│   │   ├── divergence.py           # Divergence detection and imbalance cap
│   │   └── drivers.py              # Ranked driver extraction → structured driver schema
│   │
│   ├── confidence/                 # Layer 09
│   │   ├── staleness.py            # Staleness thresholds per signal type
│   │   └── scorer.py               # Penalty system, confidence score 0–100
│   │
│   ├── explanation/                # Layer 10
│   │   ├── templates.py            # Rule-based templates (Phase 1, free tier)
│   │   └── gpt.py                  # GPT-4o-mini grounded on driver schema (Phase 2, Pro tier)
│   │
│   └── persistence/
│       ├── redis_writer.py         # Writes current state to Redis
│       └── pg_writer.py            # Appends to sentiment_history, price_snapshots
│
├── db/                             # Database access layer
│   ├── __init__.py
│   ├── connection.py               # PostgreSQL connection pool (asyncpg)
│   ├── redis.py                    # Redis connection
│   └── queries/
│       ├── raw_signals.py          # Insert and 90-day history queries
│       ├── raw_articles.py         # Insert, hash lookup, cluster queries
│       ├── sentiment_history.py    # Insert and history read queries
│       ├── price_snapshots.py      # Insert queries
│       ├── backtest.py             # Forward return population and read queries
│       ├── api_keys.py             # Key validation and tier lookup
│       └── universe.py             # Active ticker universe queries
│
├── backfill/                       # One-time pre-launch scripts
│   ├── ohlcv_backfill.py           # Alpha Vantage 2yr OHLCV → raw_signals
│   ├── indicators_backfill.py      # RSI, technicals → raw_signals
│   ├── edgar_backfill.py           # Form 4 history → raw_signals
│   ├── analyst_backfill.py         # Finnhub analyst history → raw_signals
│   ├── news_backfill.py            # NewsAPI 30-day articles → raw_articles
│   └── universe_seed.py            # Populate ticker_universe table
│
├── validation/                     # Offline signal quality checks
│   └── signal_validator.py         # Sub-index vs forward return correlation checks
│
└── tests/
    ├── test_api.py
    ├── test_scoring.py
    ├── test_features.py
    └── test_fallback.py
```

---

---

# Artifact 4 — Phase 1 Task List in Execution Order

Tasks are sequenced so that each one has its dependencies already in place. Nothing is blocked waiting for something built later.

---

### Block 1 — Foundation (do this first, everything depends on it)

```
1.  Create Railway project
    - Provision PostgreSQL database
    - Provision Redis instance
    - Set environment variables (.env.example as template)

2.  Initialise repo structure
    - Create folder layout from Artifact 3
    - requirements.txt with: fastapi, uvicorn, asyncpg, redis, apscheduler,
      httpx, pydantic, numpy, scikit-learn, sentence-transformers, python-dotenv

3.  Run SQL migrations in order
    - Migration 001: raw_signals
    - Migration 002: raw_articles
    - Migration 003: sentiment_history, price_snapshots, backtest_results
    - Migration 004: api_keys, ticker_universe

4.  Implement db/connection.py and db/redis.py
    - asyncpg connection pool
    - Redis client wrapper
    - Test both connections
```

---

### Block 2 — Active Universe and Backfill

```
5.  Seed ticker universe
    - Run backfill/universe_seed.py
    - Populate ticker_universe with top 500 liquid US equities as tier1_supported
    - Verify db/queries/universe.py can query active tickers

6.  Run OHLCV backfill
    - backfill/ohlcv_backfill.py
    - Pull 2yr daily bars for all tier1 tickers from Alpha Vantage
    - Write to raw_signals with upload_type = 'manual_backfill'
    - Verify row counts in database

7.  Run technical indicators backfill
    - backfill/indicators_backfill.py
    - RSI (14-period), SMA 20/50 for all tickers
    - Write to raw_signals

8.  Run EDGAR Form 4 backfill
    - backfill/edgar_backfill.py
    - Pull last 90 days of Form 4 filings for all tickers
    - Parse net shares, actor role, transaction date
    - Write to raw_signals

9.  Run analyst history backfill
    - backfill/analyst_backfill.py
    - Pull last 90 days of rating changes from Finnhub
    - Write to raw_signals

10. Run news backfill
    - backfill/news_backfill.py
    - Pull 30 days of articles from NewsAPI and Alpha Vantage NEWS_SENTIMENT
    - Write to raw_articles with content_hash for dedup
    - Verify no duplicate hashes in table
```

---

### Block 3 — Feature Engineering Layer

```
11. Implement features/zscore.py
    - Query raw_signals for last 90 days per signal per ticker
    - Compute mean and std
    - Return z-score with fallback logic:
        30–89 days: compute with penalty flag
        10–29 days: cross-sectional against GICS sector + market-cap bucket
        <10 days:   return None (signal excluded)

12. Implement features/scaling.py
    - clip(z, -3, +3)
    - scale to 0–100
    - inversion flag for bearish-direction signals

13. Implement features/decay.py
    - Half-life config: market 1hr, news 24hr, analyst 168hr, insider 720hr, social 4hr
    - Return w_time = e^(-λ × age_hours)

14. Implement features/weighting.py
    - Source weight config dictionary
    - Combine: w = w_source × w_confidence × w_time

15. Implement features/shrinkage.py
    - score = 50 + min(1.0, n_signals / 5) × (raw_score − 50)

16. Write unit tests for all feature functions
    - Test z-score with known distributions
    - Test scaling boundary values (z=-3, z=0, z=+3)
    - Test decay at 0hrs, half-life, 2× half-life
```

---

### Block 4 — Data Source Fetchers

```
17. Implement pipeline/sources/market.py
    - Alpha Vantage: daily OHLCV, RSI, SMA
    - Finnhub: put/call ratio, short interest, implied volatility
    - Compute: 1d/5d/20d returns, volume ratio
    - Write new raw values to raw_signals (upload_type: live)
    - Return dict of signal_type → {value, timestamp, source}

18. Implement pipeline/sources/influencer.py
    - SEC EDGAR: fetch recent Form 4s per ticker
    - Parse: net shares, actor role, transaction date
    - Finnhub: fetch recent analyst rating changes
    - Parse: old rating, new rating, delta, target price change
    - Write to raw_signals
    - Return dict of signals

19. Implement pipeline/sources/narrative.py
    - Alpha Vantage NEWS_SENTIMENT: fetch latest articles per ticker
    - NewsAPI: fetch latest articles per ticker
    - ApeWisdom: fetch mention count and sentiment (opportunistic)
    - Hash-check each article against raw_articles before insert
    - Write new articles to raw_articles
    - Return list of article objects with provider_sentiment and relevance_score

20. Implement pipeline/sources/macro.py
    - Alpha Vantage / Finnhub: fetch VIX current value
    - Alpha Vantage: fetch sector ETF close for relevant sector
    - Compute: 20-day sector ETF return, VIX z-score
    - Write to raw_signals
    - Return dict of signals
```

---

### Block 5 — Sub-Index Aggregation

```
21. Implement pipeline/scoring/subindices.py
    - For each layer, receive dict of normalized weighted scores
    - Apply weighted average formula
    - Apply shrinkage
    - Return sub-index value (0–100) + n_signals + source list

22. Implement pipeline/nlp/dedup.py
    - Stage 1: content_hash exact match (already handled at insert)
    - Stage 2: cluster articles within 2–4 hour window by ticker
    - Compute cosine similarity on titles using sentence-transformers
    - If similarity > 0.85: assign same event_cluster_id, keep highest relevance_score article
    - Update event_cluster_id in raw_articles

23. Implement pipeline/scoring/drivers.py
    - After sub-index computation, extract top N signals by weight × magnitude
    - Return ranked list using driver schema:
      {signal, description, direction, magnitude, source_layer, confidence}
```

---

### Block 6 — Composite Engine and Confidence

```
24. Implement pipeline/scoring/composite.py
    - Weights: market 0.35, narrative 0.30, influencer 0.25, macro 0.10
    - Redistribute weights if any layer is None
    - Return composite score

25. Implement pipeline/scoring/divergence.py
    - Compute spread across available sub-indices
    - Flag: spread > 40 = high_divergence, > 20 = moderate_divergence
    - Apply cap: if any layer > 85 and any layer < 30, cap composite at 75

26. Implement pipeline/confidence/staleness.py
    - Staleness threshold config:
        market: 90 min, news: 6hr, analyst: 3 days, insider: 30 days, macro: 1 day
    - Compare each source's as_of timestamp against threshold
    - Return stale flags per source

27. Implement pipeline/confidence/scorer.py
    - Start at 100
    - Apply penalties: missing layer -15, stale -10, low volume -20, high divergence -15
    - Clip to [0, 100]
    - Return integer confidence score + list of active flags
```

---

### Block 7 — Explanation and Persistence

```
28. Implement pipeline/explanation/templates.py
    - Template map keyed on driver signal type
    - Input: ranked driver list from drivers.py
    - Output: plain-English explanation string
    - Must only reference signals present in driver list

29. Implement pipeline/persistence/redis_writer.py
    - Serialize full scored state to JSON
    - Write to key sentiment:{ticker}
    - Set long TTL (do not expire aggressively)

30. Implement pipeline/persistence/pg_writer.py
    - Insert row into sentiment_history
    - Insert row into price_snapshots
    - Both inserts in single transaction
```

---

### Block 8 — Orchestration and Scheduling

```
31. Implement pipeline/orchestrator.py
    - asyncio.gather() for parallel source fetching (Layers 03–06)
    - Per-layer fallback propagation:
        primary fails → try fallback
        fallback fails → use last known value if within staleness threshold
        stale beyond threshold → mark layer missing
    - Merge results, pass to feature layer
    - Call sub-index aggregators
    - Call composite engine
    - Call confidence scorer
    - Call explanation generator
    - Call redis_writer and pg_writer

32. Implement pipeline/scheduler.py
    - Register four APScheduler jobs:
        market_job:      CronTrigger(day_of_week='mon-fri', hour='9-16', minute='*/15')
        narrative_job:   IntervalTrigger(minutes=30)
        influencer_job:  IntervalTrigger(hours=6)
        macro_job:       CronTrigger(hour=2)
    - Each job calls orchestrator with the appropriate layer scope
    - Scheduler starts on app startup
```

---

### Block 9 — API Layer

```
33. Implement api/auth.py
    - Hash incoming Bearer token
    - Look up hash in api_keys table
    - Return tier or raise 401

34. Implement api/rate_limit.py
    - Per-tier limits: free 10/min, pro 120/min
    - Use Redis for request counting per key

35. Implement api/response/schemas.py
    - Pydantic models for all response shapes:
        FreeTierResponse, ProTierResponse, NoDataResponse, ErrorResponse

36. Implement api/response/labels.py
    - score_to_label(score: int) → str
    - 0-20: Strongly Bearish, 21-40: Bearish, 41-60: Neutral,
      61-80: Bullish, 81-100: Strongly Bullish

37. Implement api/response/assembler.py
    - Read from Redis key sentiment:{ticker}
    - If key missing: query sentiment_history for last record
    - If no record: return NoDataResponse
    - Apply tier filtering (strip sub_indices, drivers, freshness for free tier)
    - Return appropriate schema

38. Implement api/routes/sentiment.py
    - GET /v1/sentiment/{ticker}
    - Validate ticker against universe table
    - Call assembler
    - If refresh=true: queue background job

39. Implement api/routes/history.py
    - GET /v1/sentiment/{ticker}/history
    - Pro tier only — return 401 for free tier
    - Query sentiment_history with days and interval params

40. Implement api/routes/tickers.py and api/routes/status.py
    - /v1/tickers: query ticker_universe
    - /v1/status: query last run timestamps from Redis or pg

41. Implement main.py
    - Mount all routes
    - Start scheduler on startup event
    - Health check endpoint GET /health
```

---

### Block 10 — Testing and Pre-Launch

```
42. Write test suite
    - test_features.py: z-score, scaling, decay, weighting, shrinkage
    - test_scoring.py: sub-index aggregation, composite, divergence, confidence
    - test_api.py: auth, rate limits, response schemas, no-data cases
    - test_fallback.py: simulate source failures, verify graceful degradation

43. Run validation script
    - validation/signal_validator.py
    - For each sub-index: compute correlation with 1d and 5d forward returns
    - Log results — flag any sub-index with near-zero correlation before launch

44. End-to-end smoke test
    - Trigger orchestrator manually for 5 tickers
    - Verify: raw_signals written, sentiment_history written, Redis updated
    - Call API endpoint, verify response schema matches contract
    - Simulate source failure, verify fallback behaviour and confidence penalty

45. Deploy to Railway
    - Set all environment variables
    - Deploy FastAPI app
    - Verify scheduler starts on boot
    - Verify /health endpoint responds
    - Verify /v1/status shows recent run timestamps
```