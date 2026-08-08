"""Composite conversation-conviction score + sizing hooks.

Pure functions. The composite is *unsigned* (direction comes from the
cohort wallet; see schema.py docstring). Weights follow the equity lane's
validated attention-led shape: mention-volume anomaly dominates, the rest
refines.

    composite = 0.50 * sigmoid(attention_z)          # anomaly vs own history
              + 0.25 * sigmoid(log2(velocity))       # rate expanding/contracting
              + 0.15 * engagement                    # comments per post
              + 0.10 * breadth                       # distinct voices

All four terms live in [0, 1]; a perfectly average conversation scores
about 0.5 * 0.5 + 0.25 * 0.5 + small ~= 0.4-0.5, i.e. a ~1.0x multiplier.

Sizing hook: `conviction_multiplier` maps (composite, confidence) to a
bounded size multiplier. **Missing/stale signal must map to exactly 1.0
at the call site** — the layer degrades to a no-op, never to a veto.
"""

from __future__ import annotations

import math
from typing import Any

from polysim.signals.extract import sigmoid
from polysim.signals.schema import MarketSignal

W_ATTENTION = 0.50
W_VELOCITY = 0.25
W_ENGAGEMENT = 0.15
W_BREADTH = 0.10


def compose_conviction(
    *,
    attention_z: float,
    velocity: float,
    engagement: float,
    breadth: float,
) -> tuple[float, dict[str, Any]]:
    """Blend features into an unsigned composite ∈ [0, 1].

    Returns (composite, components) — components carry each term so the
    stored row is auditable/reproducible.
    """
    a = sigmoid(attention_z)
    v = sigmoid(math.log2(velocity) if velocity > 0 else -3.0)
    e = min(1.0, max(0.0, engagement))
    b = min(1.0, max(0.0, breadth))
    composite = (
        W_ATTENTION * a + W_VELOCITY * v + W_ENGAGEMENT * e + W_BREADTH * b
    )
    composite = min(1.0, max(0.0, composite))
    components = {
        "term_attention": round(a, 4),
        "term_velocity": round(v, 4),
        "term_engagement": round(e, 4),
        "term_breadth": round(b, 4),
        "attention_z": round(attention_z, 4),
        "velocity": round(velocity, 4),
    }
    return composite, components


def signal_confidence(
    *,
    n_matched_posts: int,
    baseline_points: int,
    min_matched_posts: int = 3,
    min_baseline_points: int = 5,
    community_fallback: bool = False,
) -> float:
    """Data-sufficiency confidence ∈ [0, 1].

    * post coverage: how many relevant posts the score rests on (market-term
      matches normally; community posts when falling back to the tide)
    * baseline coverage: how much history the z-score rests on
    * community_fallback: no term matched → we scored the community tide
      only, so confidence is capped at 0.5.
    """
    match_cov = min(1.0, n_matched_posts / max(1, min_matched_posts))
    base_cov = min(1.0, baseline_points / max(1, min_baseline_points))
    conf = match_cov * base_cov
    if community_fallback:
        conf = min(conf, 0.5)
    return min(1.0, max(0.0, conf))


def conviction_multiplier(
    signal: MarketSignal | None,
    *,
    min_mult: float = 0.5,
    max_mult: float = 1.5,
) -> float:
    """Bounded size multiplier from a signal; 1.0 when signal is absent.

    The raw map is linear in composite; confidence pulls the result back
    toward neutral so thin data can't swing size much:

        raw       = min_mult + (max_mult - min_mult) * composite
        effective = 1.0 + (raw - 1.0) * confidence
    """
    if signal is None:
        return 1.0
    lo = min(min_mult, max_mult)
    hi = max(min_mult, max_mult)
    raw = lo + (hi - lo) * min(1.0, max(0.0, signal.composite))
    conf = min(1.0, max(0.0, signal.confidence))
    return 1.0 + (raw - 1.0) * conf


def signal_gate_blocks(
    signal: MarketSignal | None,
    *,
    min_composite: float = 0.15,
    min_confidence: float = 0.5,
) -> bool:
    """True iff a confident signal says the conversation is dead.

    Fail-open by design: no signal, stale signal (caller's job), or low
    confidence → False (trade proceeds unmodified).
    """
    if signal is None:
        return False
    if signal.confidence < min_confidence:
        return False
    return signal.composite < min_composite


__all__ = [
    "compose_conviction",
    "conviction_multiplier",
    "signal_confidence",
    "signal_gate_blocks",
]
