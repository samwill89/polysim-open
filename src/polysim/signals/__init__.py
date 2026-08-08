"""External conversation-signal layer.

Public conversation activity (Reddit today) → per-market conviction score
→ bounded sizing multiplier + optional dead-conversation gate, measured
via pre-registered tournament variants. See docs/signals.md.
"""

from polysim.signals.schema import ConversationPost, MarketSignal, TopicActivity
from polysim.signals.scoring import (
    compose_conviction,
    conviction_multiplier,
    signal_confidence,
    signal_gate_blocks,
)

__all__ = [
    "ConversationPost",
    "MarketSignal",
    "TopicActivity",
    "compose_conviction",
    "conviction_multiplier",
    "signal_confidence",
    "signal_gate_blocks",
]
