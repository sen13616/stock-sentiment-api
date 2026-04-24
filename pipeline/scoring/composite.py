"""
pipeline/scoring/composite.py

Combines four layer sub-indices into a single composite score (0–100).

Layer weights (spec §Layer 08)
-------------------------------
    Market      0.35
    Narrative   0.30
    Influencer  0.25
    Macro       0.10

Missing layers
--------------
If a layer sub-index is None, its weight is redistributed proportionally
across the remaining present layers so the weights still sum to 1.0.
When ALL layers are missing the function returns the neutral score (50.0).
"""
from __future__ import annotations

from dataclasses import dataclass, field

LAYER_WEIGHTS: dict[str, float] = {
    "market":     0.35,
    "narrative":  0.30,
    "influencer": 0.25,
    "macro":      0.10,
}


@dataclass(frozen=True)
class CompositeResult:
    score: float                         # weighted composite, 0–100
    weights_used: dict[str, float]       # effective (redistributed) weights
    missing_layers: list[str] = field(default_factory=list, compare=False)


def compute_composite(
    sub_indices: dict[str, object | None],
) -> CompositeResult:
    """
    Compute composite score from layer sub-indices.

    Parameters
    ----------
    sub_indices : dict mapping layer name → SubIndexResult (or any object
                  with a `.value` attribute) | None.
                  Expected keys: "market", "narrative", "influencer", "macro".
                  A None value means the layer is missing.

    Returns
    -------
    CompositeResult with `score`, `weights_used`, and `missing_layers`.
    """
    # Accept either SubIndexResult objects (has .value) or raw floats
    def _value(v: object) -> float:
        return float(v.value) if hasattr(v, "value") else float(v)  # type: ignore[union-attr]

    present  = {k: v for k, v in sub_indices.items() if v is not None}
    missing  = [k for k, v in sub_indices.items() if v is None]

    # Also treat keys from LAYER_WEIGHTS not present in sub_indices as missing
    for layer in LAYER_WEIGHTS:
        if layer not in sub_indices and layer not in missing:
            missing.append(layer)

    if not present:
        return CompositeResult(score=50.0, weights_used={}, missing_layers=missing)

    total_w = sum(LAYER_WEIGHTS.get(k, 0.0) for k in present)
    if total_w == 0:
        return CompositeResult(score=50.0, weights_used={}, missing_layers=missing)

    weights_used = {k: LAYER_WEIGHTS.get(k, 0.0) / total_w for k in present}
    score = sum(weights_used[k] * _value(v) for k, v in present.items())

    return CompositeResult(
        score=round(score, 4),
        weights_used=weights_used,
        missing_layers=sorted(missing),
    )
