"""Wallet discovery pipeline — empirical-priors addendum §3.

Nightly:
  1. poly_data_sync       — clone/pull warproxxx/poly_data, ensure freshness
  2. features             — Polars-driven per-(wallet, scope) feature rows
  3. niche_tags           — multi-label market tagger (aec/ai_labs/creator_econ)
  4. classifier           — pure-feature Tier-1 edge_likelihood scorer
  5. cohort               — frozen-per-experiment top-N selection

Entry points: see `polysim discovery {run,show-cohort,coverage}` in cli.py.
"""

from polysim.discovery.niche_tags import NICHE_TAGS_VERSION, tag_market

__all__ = ["NICHE_TAGS_VERSION", "tag_market"]
