-- ============================================================
-- Migration 007 — FinBERT class probabilities + language column
--                 (Sprint A, Phase 2 items 2.1 + 2.2)
--
-- finbert_score already exists from migrations.sql (all NULLs).
-- This adds the three class probability columns needed for
-- w_conf computation, and a language column for English-only
-- filtering before FinBERT inference.
-- ============================================================

ALTER TABLE raw_articles
    ADD COLUMN IF NOT EXISTS finbert_pos DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS finbert_neg DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS finbert_neu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS language VARCHAR(10);
