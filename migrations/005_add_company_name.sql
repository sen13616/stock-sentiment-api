-- ============================================================
-- Migration 005 — Add company_name to ticker_universe
-- ============================================================

ALTER TABLE ticker_universe
    ADD COLUMN IF NOT EXISTS company_name VARCHAR(200);
