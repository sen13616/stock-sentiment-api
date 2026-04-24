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
    content_hash        VARCHAR(64),
    event_cluster_id    VARCHAR(100),
    finbert_score       FLOAT,
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
    key_hash     VARCHAR(128) NOT NULL UNIQUE,
    tier         VARCHAR(20)  NOT NULL CHECK (tier IN ('free', 'pro')),
    owner_email  VARCHAR(255),
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE ticker_universe (
    id                    SERIAL PRIMARY KEY,
    ticker                VARCHAR(10)  NOT NULL UNIQUE,
    tier                  VARCHAR(20)  NOT NULL CHECK (tier IN ('tier1_supported', 'tier2_requested')),
    added_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_requested_at     TIMESTAMPTZ
);

CREATE INDEX idx_ticker_universe_tier
ON ticker_universe (tier);