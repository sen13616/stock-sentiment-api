"""
pipeline/scoring/subindices.py

Computes a single sub-index value (0–100) from a collection of pre-scored,
pre-weighted signals belonging to one data layer.

Formula (spec §Mathematics)
---------------------------
    raw   = Σ(w_i × score_i) / Σ(w_i)         weighted average
    value = 50 + min(1, n/5) × (raw − 50)      volume shrinkage toward neutral

The shrinkage factor `min(1, n/5)` pulls the result toward 50 when fewer
than 5 signals are present, guarding against single-signal overconfidence.

Expected input per signal dict
-------------------------------
    score  : float in [0, 100]  — z-scaled, direction-corrected (50 = neutral)
    weight : float > 0          — combined w_source × w_confidence × w_time
    source : str                — data source label

Returns None (missing layer) when the signal list is empty or all weights
are zero.  The orchestration layer redistributes composite weight accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SubIndexResult:
    value: float          # shrinkage-adjusted sub-index, 0–100
    n_signals: int        # number of valid contributing signals
    sources: list[str] = field(default_factory=list, compare=False)


def compute_sub_index(signals: list[dict]) -> SubIndexResult | None:
    """
    Compute a layer sub-index from pre-scored, pre-weighted signal dicts.

    Parameters
    ----------
    signals : list[dict]
        Each dict must contain:
          - 'score'  (float) : 0–100, direction-corrected, 50 = neutral
          - 'weight' (float) : combined w_source × w_confidence × w_time
          - 'source' (str)   : data source label

    Returns
    -------
    SubIndexResult or None when no valid signals exist (missing layer).
    """
    valid = [s for s in signals if (s.get("weight") or 0) > 0]
    if not valid:
        return None

    total_w = sum(s["weight"] for s in valid)
    if total_w == 0:
        return None

    raw = sum(s["score"] * s["weight"] for s in valid) / total_w

    n = len(valid)
    shrinkage = min(1.0, n / 5)
    value = 50.0 + shrinkage * (raw - 50.0)

    sources = sorted({s["source"] for s in valid})
    return SubIndexResult(
        value=round(value, 4),
        n_signals=n,
        sources=sources,
    )
