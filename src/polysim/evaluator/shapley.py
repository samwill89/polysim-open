"""Detector attribution — build plan §5.3.

Spec §7.7 calls for a "Shapley-style contribution of each detector to
run P&L". Full Shapley over K=5 detectors is 2^5 = 32 subsets, which is
tractable, but the marginal-contribution game function isn't well-defined
without replaying the whole scorer under each subset (we'd need a ground-
truth P&L per subset — circular for a live run).

We therefore use **proportional attribution**: each closed position's
realized_pnl is split across the detectors that produced the triggering
flag, weighted by each detector's `contribution_by_detector` share
(weight * raw_score * confidence). This agrees with full Shapley when
the value function is linear in detector contributions — which is how
the composite scorer is specified (sum of weighted raw*conf). Differs
only when detectors interact non-linearly, which the composite scorer
does not.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from polysim.evaluator.metrics import _attribute_pnl_by_detector  # re-export


def attribute_closed_positions(
    closed_positions: Iterable[Mapping[str, Any]],
    flags_by_id: Mapping[int, Mapping[str, Any]],
) -> dict[str, int]:
    """Proportional attribution over an arbitrary positions slice.

    `flags_by_id` maps source_flag_id -> flag row (must have components_json).
    Returns {detector_name: cents} dict, signed.
    """
    per_detector: dict[str, int] = defaultdict(int)
    for pos in closed_positions:
        fid = pos.get("source_flag_id")
        if fid is None:
            continue
        flag = flags_by_id.get(int(fid))
        if flag is None:
            continue
        components_raw = flag.get("components_json")
        components: dict[str, Any] | None = None
        if isinstance(components_raw, (str, bytes)):
            try:
                parsed = json.loads(components_raw)
                if isinstance(parsed, dict):
                    components = parsed
            except json.JSONDecodeError:
                components = None
        elif isinstance(components_raw, Mapping):
            components = dict(components_raw)
        if components is None:
            continue
        contrib = components.get("contribution_by_detector")
        if not isinstance(contrib, dict) or not contrib:
            continue
        total = 0.0
        for v in contrib.values():
            if isinstance(v, (int, float)):
                total += float(v)
        if total <= 0.0:
            continue
        pnl = int(pos.get("realized_pnl_cents") or 0)
        for det, c in contrib.items():
            try:
                share = int(pnl * (float(c) / total))
            except (TypeError, ValueError):
                continue
            per_detector[str(det)] += share
    return dict(per_detector)


__all__ = ["_attribute_pnl_by_detector", "attribute_closed_positions"]
