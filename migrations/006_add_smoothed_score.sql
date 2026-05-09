-- ============================================================
-- Migration 006 — EMA smoothing columns (Sprint 5a, G-C3)
--
-- ALREADY APPLIED to Railway production on 2026-05-09.
-- This file exists for reproducibility only — do NOT re-run.
-- ============================================================

ALTER TABLE sentiment_history
    ADD COLUMN composite_score_smoothed DOUBLE PRECISION,
    ADD COLUMN ema_obs_count INTEGER NOT NULL DEFAULT 0;
