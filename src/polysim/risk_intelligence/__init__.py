"""Evidence-aware pre-trade controls for paper execution."""

from polysim.risk_intelligence.gates import (
    CorrelationContext,
    RiskDecision,
    evaluate_trade_risk,
)
from polysim.risk_intelligence.schema import (
    EvidenceArticle,
    EvidenceAssessment,
    EvidenceSource,
)

__all__ = [
    "CorrelationContext",
    "EvidenceArticle",
    "EvidenceAssessment",
    "EvidenceSource",
    "RiskDecision",
    "evaluate_trade_risk",
]
