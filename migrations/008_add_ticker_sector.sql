-- Migration 008 — Add sector column to ticker_universe (Sprint P4.1, M5)
--
-- Required for per-ticker macro routing (P4.2). Additive only; safe to apply
-- live. Idempotent via IF NOT EXISTS.
--
-- Apply:
--     psql $DATABASE_URL < migrations/008_add_ticker_sector.sql
--
-- Reverse (if needed):
--     ALTER TABLE ticker_universe DROP COLUMN sector;

ALTER TABLE ticker_universe
    ADD COLUMN IF NOT EXISTS sector VARCHAR(50);

COMMENT ON COLUMN ticker_universe.sector IS
    'GICS sector name (11 classes). Joins to pipeline.sources.macro.SECTOR_ETFS for per-ticker macro routing (Phase 4, Sprint P4.1).';
