# Audit C — Composite Construction Post-Phase-1 Re-Run

**Date:** 2026-05-10
**Original audit:** 2026-05-07
**Auditor:** Claude Code
**Sprints deployed since original:** 1, 2, 3, 4, 5a, 6

---

## Summary

All three Critical and Significant gaps that were targeted for Phase 1 resolution (C1, C2, S1, S3) are now **RESOLVED**, verified against current code on the `main` branch (HEAD commit `1b658cc`). Two Significant gaps (S4, S5) and one Cosmetic gap (K3) remain **OPEN** as expected (Phase 3 / low priority). One Significant gap (S2) remains **OPEN** and documented as intentional. One Cosmetic gap (K1) is **RESOLVED**. One Cosmetic gap (K2) remains **ACCEPTED** as future work. No regressions were found.

The scoring architecture has been fundamentally restructured. The per-job scoring model with carry-forward has been replaced by a clean separation: ingestion jobs are data-only, and a dedicated `scoring_tick_job` runs every 30 minutes, recomputing all four layers for all tickers from current DB state. EMA smoothing has been implemented with the paper's specified 4-hour half-life formula, cold-start semantics, and full persistence through both Redis and PostgreSQL. The API correctly serves the smoothed value as `score` and the unsmoothed value as `score_raw` (pro tier only).

---

## Gap Resolution Table

| Original ID | Gap Description | Original Severity | Sprint Fix | Current Status | Evidence |
|---|---|---|---|---|---|
| C1 | EMA smoothing absent | Critical | Sprint 5a | **RESOLVED** | `pipeline/scoring/ema.py` implements `alpha = 1 - 0.5^(dt/T_half)` with T_half = 4h. Cold-start returns raw. Schema migration `006_add_smoothed_score.sql` adds `composite_score_smoothed` and `ema_obs_count` columns. Orchestrator integrates EMA at lines 332-349. API assembler serves smoothed as `score`, raw as `score_raw`. 23 unit tests in `tests/test_ema_smoothing.py`. |
| C2 | No global scoring tick | Critical | Sprint 3 | **RESOLVED** | `scoring_tick_job()` at `pipeline/scheduler.py:526-557` runs every 30 min via `IntervalTrigger(minutes=30)`. All six ingestion jobs (market, market_eod, narrative, influencer, macro, short_volume) are data-only -- none call `_score_all()` or `_score_and_write()`. `_carry_forward_layer()` has been removed from `orchestrator.py`. |
| S1 | Carry-forward no staleness bound | Significant | Sprint 3 | **RESOLVED** | Eliminated by C2 fix. `_carry_forward_layer()` no longer exists. All four layers are freshly recomputed from DB state on every scoring tick. The `_fallback_subindex()` function (with staleness bounds) remains as the only fallback path, used when a layer has no fresh data in the DB. |
| S2 | Divergence cap no paper basis | Significant | -- | **OPEN (intentional)** | `pipeline/scoring/divergence.py:68-69` still applies `min(composite, 75.0)` when any layer > 85 and any < 30. This remains an engineering guardrail with no paper basis. Documented as intentional in CLAUDE.md and the codebase. No change since original audit. |
| S3 | sentiment_history row volume high | Significant | Sprint 3 | **RESOLVED** | With ingestion jobs decoupled from scoring, rows are now written only by `scoring_tick_job` (every 30 min) = 48 rows/ticker/day. Market job (15-min intervals) no longer triggers scoring or writes to sentiment_history. |
| S4 | Confidence operational not statistical | Significant | -- | **OPEN (Phase 3)** | `pipeline/confidence/scorer.py` still uses penalty-based operational confidence (100 minus deductions). No bootstrap confidence intervals. This is intentional for Phase 1 -- the operational confidence serves API consumers; statistical confidence is deferred to Phase 3. |
| S5 | Redis-Postgres non-atomic | Significant | -- | **OPEN (low priority)** | `pipeline/orchestrator.py:416-417` still writes Redis first (`write_scored_state`), then Postgres (`persist_scored_state`) as two independent async operations. Self-healing within 30 min. No change since original audit. |
| K1 | CLAUDE.md stale weights | Cosmetic | Sprint 4 era | **RESOLVED** | CLAUDE.md line 50 now reads: "Weighted average of 4 sub-indices (market 0.35, narrative 0.30, influencer 0.25, macro 0.10)". Matches `composite.py:23-28`. |
| K2 | No regime adaptation | Cosmetic | -- | **ACCEPTED (future)** | No regime-conditioned weights. VIX is ingested but not used for weight conditioning. Accepted as future work per original audit. |
| K3 | Divergence cap_applied not persisted | Cosmetic | -- | **OPEN** | `sentiment_history.divergence` column still stores only the flag string ("aligned"/"moderate_divergence"/"high_divergence"). `cap_applied` boolean is computed in `DivergenceResult` but not written to DB or Redis state. The `state` dict at `orchestrator.py:408` writes `"divergence": div_result.flag` only. |

---

## Detailed Findings (changed status only)

### C1: EMA Smoothing — RESOLVED

**Sprint 5a implementation verified across five layers:**

**1. EMA computation (`pipeline/scoring/ema.py:1-63`)**

The module implements the paper's formula exactly:

```python
alpha = 1.0 - math.pow(0.5, dt_hours / half_life_hours)
return alpha * raw_t + (1.0 - alpha) * prev_smoothed
```

- Half-life constant: `_HALF_LIFE_HOURS = 4.0` (line 30) -- matches paper's "4-hour half-life".
- Cold-start: when `prev_smoothed is None`, returns `raw_t` unchanged (line 53-54) -- seeds EMA with first raw composite as specified.
- Zero dt: returns `prev_smoothed` unchanged (line 59-60) -- prevents division-by-zero edge case.
- Negative dt: clamped to 0.0 (line 57) -- defensive guard.
- Large dt behavior: as `dt -> infinity`, `alpha -> 1`, so `smoothed -> raw`. After 24h gap (6 half-lives), alpha = 0.984, effectively resetting. No threshold-based reset logic -- handled naturally by the formula. This matches the paper's description.

**2. Orchestrator integration (`pipeline/orchestrator.py:332-349`)**

```python
prev_smoothed: float | None = None
prev_obs_count: int = 0
dt_hours: float = 0.0

if last_state:
    prev_smoothed  = last_state.get("composite_score_smoothed")
    if prev_smoothed is None:
        prev_smoothed = last_state.get("composite_score")
    prev_obs_count = int(last_state.get("ema_obs_count") or 0)
    prev_ts        = _parse_ts(last_state.get("timestamp"))
    if prev_ts is not None:
        dt_hours = max(0.0, (now - prev_ts).total_seconds() / 3600.0)

smoothed_score = compute_ema(effective_score, prev_smoothed, dt_hours)
ema_obs_count  = prev_obs_count + 1
```

Key observations:
- EMA is applied to `effective_score` (post-divergence-cap composite), not the raw composite. This is correct -- the divergence cap is upstream of smoothing.
- `prev_smoothed` falls back to `composite_score` if `composite_score_smoothed` is absent (for pre-Sprint-5a Redis states). This ensures backward compatibility during rollout.
- `ema_obs_count` is monotonically increasing (never resets), starting at 1 on cold-start. This matches the module docstring specification.
- `dt_hours` is computed from the actual elapsed time between scoring ticks, not assumed to be 0.5h. This handles irregular tick intervals correctly (variable-timestep EMA).

**3. State assembly and persistence (`pipeline/orchestrator.py:375-417`)**

The state dict uses a dual-key scheme:
- `composite_score` = smoothed value (served as API `score`)
- `composite_score_raw` = unsmoothed value (served as API `score_raw`, pro only)
- `composite_score_smoothed` = mirrors `composite_score` for the PG writer

The PG writer (`pipeline/persistence/pg_writer.py:107-119`) correctly maps:
- DB `composite_score` column = raw value (`composite_score_raw` from state)
- DB `composite_score_smoothed` column = smoothed value
- DB `ema_obs_count` column = counter

The `insert_row` function in `db/queries/sentiment_history.py:39-85` accepts `composite_score_smoothed` and `ema_obs_count` as parameters and writes them to the INSERT statement (lines 61-62, 83-84).

**4. Schema (`migrations/006_add_smoothed_score.sql`)**

```sql
ALTER TABLE sentiment_history
    ADD COLUMN composite_score_smoothed DOUBLE PRECISION,
    ADD COLUMN ema_obs_count INTEGER NOT NULL DEFAULT 0;
```

Both columns exist. `composite_score_smoothed` is nullable (correct -- pre-EMA historical rows have NULL). `ema_obs_count` defaults to 0 (correct -- pre-EMA rows have 0 updates).

Note: the base `migrations.sql` schema (lines 57-75) does NOT include these columns. They exist only via migration 006. This is consistent with the migration comment "ALREADY APPLIED to Railway production on 2026-05-09."

**5. API layer (`api/response/assembler.py:192-254`, `api/response/schemas.py:75-91`)**

Free tier (`_build_free`, line 176): `score = int(round(state.get("composite_score")))` -- serves the smoothed value (since `composite_score` in Redis state = smoothed).

Pro tier (`_build_pro`, lines 229-235):
```python
raw_val = state.get("composite_score_raw")
score_raw = int(round(raw_val)) if raw_val is not None else None
obs_count = state.get("ema_obs_count")
ema_obs_count = int(obs_count) if obs_count is not None else None
```

The `ProTierResponse` schema includes `score_raw: Optional[int]` (line 78) and `ema_obs_count: Optional[int]` (line 79), both with comments indicating their purpose.

DB fallback path (`_load_from_db`, lines 102-108): correctly maps DB columns to Redis state shape:
```python
smoothed = row.get("composite_score_smoothed")
raw_score = row["composite_score"]
return {
    "composite_score": smoothed if smoothed is not None else raw_score,
    "composite_score_raw": raw_score,
    ...
}
```
This handles pre-EMA rows gracefully (smoothed is None, so `composite_score` falls back to raw).

**6. Test coverage (`tests/test_ema_smoothing.py`, 23 tests)**

Tests cover: cold-start (3 tests), normal update (4 tests), half-life property (4 tests), large dt / gap behavior (3 tests), zero dt (2 tests), negative dt clamping (2 tests). Additionally, the half-life verification tests confirm the mathematical property: after exactly one half-life, the gap closes by 50% (test line 98-107: `compute_ema(100.0, 0.0, dt_hours=4.0) == 50.0`).

**Verdict: C1 is fully resolved. The EMA implementation matches the paper's specification in formula, parameterization, cold-start behavior, and persistence.**

---

### C2: Global Scoring Tick — RESOLVED

**Sprint 3 implementation verified:**

**1. `scoring_tick_job` (`pipeline/scheduler.py:526-557`)**

Runs every 30 minutes via `IntervalTrigger(minutes=30)` (line 629). The job:
- Fetches all active tickers via `get_active_tickers()`
- Calls `_score_all(tickers, "SCORING_TICK")` which invokes `_score_and_write(ticker)` for each ticker
- `_score_and_write` recomputes all four layers from current DB state (orchestrator.py:291-432)
- No `layers` parameter -- all layers are always recomputed

**2. Ingestion jobs are data-only**

Verified each ingestion job function body:
- `market_job()` (lines 286-320): calls `_fetch_all_tickers()` + `_record_run()`. No call to `_score_all` or `_score_and_write`.
- `market_eod_job()` (lines 323-357): same pattern. Data-only.
- `narrative_job()` (lines 360-434): fetch + cluster + `_record_run()`. No scoring.
- `influencer_job()` (lines 437-464): fetch + `_record_run()`. No scoring.
- `macro_job()` (lines 467-492): fetch + `_record_run()`. No scoring.
- `short_volume_job()` (lines 495-523): fetch + `_record_run()`. No scoring.

**3. `_carry_forward_layer()` eliminated**

A grep for `_carry_forward_layer` and `carry.forward` in `pipeline/` returns only one match: the orchestrator docstring at line 297 which states "no carry-forward". The function itself has been removed. All four layers are freshly read from DB and scored on every tick.

**4. `_score_and_write()` signature change**

The function at `orchestrator.py:291` now accepts only `ticker: str` -- no `layers` parameter. Lines 315-318 compute all four layers unconditionally:
```python
(market_si, ...) = await _score_market(ticker, now, last_state)
(narrative_si, ...) = await _score_narrative(ticker, now, last_state)
(influencer_si, ...) = await _score_influencer(ticker, now, last_state, current_price)
(macro_si, ...) = await _score_macro(now, last_state)
```

The `_fallback_subindex()` function remains (lines 144-184) but serves a different purpose: it recovers a sub-index from Redis when the *DB itself* has no fresh data for that layer (e.g., no narrative articles exist yet for a new ticker). This is correct fallback behavior with staleness bounds, not the old carry-forward.

**5. CLAUDE.md updated**

CLAUDE.md now documents the architecture correctly (line 33): "Ingestion jobs fetch signals from external APIs and write to DB. The `scoring_tick_job` (every 30 min) recomputes all four layers for all ~502 tickers from DB state."

**Verdict: C2 is fully resolved. The paper's "global scoring job runs every 30 minutes" architecture is now implemented.**

---

### S1: Carry-Forward No Staleness Bound — RESOLVED

Eliminated by the C2 fix. Since all four layers are freshly recomputed from DB state on every scoring tick, there is no carry-forward path. The old `_carry_forward_layer()` function has been removed. The only fallback path (`_fallback_subindex()`) applies staleness bounds per `_LAYER_LOOKBACK` thresholds before returning a cached value.

---

### S3: sentiment_history Row Volume — RESOLVED

With ingestion jobs decoupled from scoring, `sentiment_history` rows are now written exclusively by `scoring_tick_job` (every 30 min). This produces 48 rows/ticker/day, matching the paper's implied rate. The market job at 15-min intervals only writes to `raw_signals` (data ingestion), not to `sentiment_history`.

---

### K1: CLAUDE.md Stale Weights — RESOLVED

CLAUDE.md line 50 now reads: "Weighted average of 4 sub-indices (market 0.35, narrative 0.30, influencer 0.25, macro 0.10)". This matches `composite.py:23-28` exactly.

---

## Findings Unchanged

### S2: Divergence Cap No Paper Basis — OPEN (intentional)

The divergence cap at `pipeline/scoring/divergence.py:68-69` is unchanged:
```python
cap_applied = any(v > 85 for v in values) and any(v < 30 for v in values)
effective   = min(composite, 75.0) if cap_applied else composite
```

This remains an engineering guardrail with no basis in the paper. The asymmetry noted in the original audit persists: bullish composites can be capped at 75, but there is no corresponding floor for bearish composites. However, the `min()` operation means the cap only reduces scores above 75, so it cannot distort bearish scores upward.

**Status: Documented as intentional engineering decision. Not a compliance gap for Phase 1.**

### S4: Confidence Operational Not Statistical — OPEN (Phase 3)

`pipeline/confidence/scorer.py` is unchanged from the original audit. It still uses penalty-based operational confidence:
- Missing layer: -15 each
- Stale source: -10 each
- Low signal volume (< 5): -20
- High divergence: -15

No bootstrap confidence intervals have been implemented. This remains deferred to Phase 3 per the sprint plan.

### S5: Redis-Postgres Non-Atomic — OPEN (low priority)

`pipeline/orchestrator.py:416-417`:
```python
await write_scored_state(ticker, state)    # Redis first
await persist_scored_state(state)          # Postgres second
```

Still two independent async operations. Self-healing within 30 min via the next scoring tick. No change since original audit. Low priority given the self-healing property.

### K2: No Regime Adaptation — ACCEPTED (future)

No change. VIX is ingested via macro_job but not used for weight conditioning. Accepted as future work per original audit.

### K3: Divergence cap_applied Not Persisted — OPEN

`orchestrator.py:408` still writes only `"divergence": div_result.flag` to the state dict. The `DivergenceResult.cap_applied` boolean (computed at `divergence.py:68`) is not persisted to Redis or PostgreSQL. Auditing cap frequency from stored data still requires the heuristic approach described in the original audit.

---

## Regressions

**None found.**

All original findings that were expected to remain unchanged (S2, S4, S5, K2, K3) are confirmed unchanged. No new gaps were introduced by the Sprint 3, 4, 5a, or 6 implementations.

One observation worth noting: the EMA smoothing is applied to the *post-divergence-cap* composite (`effective_score`), not the raw composite. This means the divergence cap at 75 is upstream of the EMA. If the cap fires on consecutive ticks, the EMA smoothes toward 75 rather than toward the true weighted average. This is arguably correct behavior (the cap is an intentional guardrail, so smoothing should respect it), but it means the EMA cannot recover information lost to the cap. This is a design choice, not a regression.

---

## Conclusion

Phase 1 has resolved all Critical gaps (C1, C2) and the Significant gaps that were dependent on them (S1, S3). The scoring architecture now matches the paper's specification:

1. **Global scoring tick**: Every 30 minutes, all tickers are scored from current DB state. Ingestion is fully decoupled from scoring.
2. **EMA smoothing**: 4-hour half-life, variable-timestep alpha formula, cold-start seeding, monotonic observation counter. Both smoothed and raw values are persisted and served via the API.
3. **Row volume**: 48 rows/ticker/day, matching the paper's implied rate.
4. **Carry-forward eliminated**: No stale sub-indices are silently passed through.
5. **Documentation**: CLAUDE.md reflects current weights and architecture.

Remaining open items (S2, S4, S5, K3) are either intentional design decisions or deferred to later phases. K2 (regime adaptation) is accepted as future work. None of the remaining gaps affect the paper's core validation methodology -- the smoothed composite signal `St` can now be validated against returns as the paper describes.
