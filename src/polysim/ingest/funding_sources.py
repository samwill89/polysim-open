"""Funding-source heuristic — classify a wallet's first inbound transfer.

Spec gap G2. Build plan §1.8.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class FundingSources:
    """Lookup: lowercase address -> source name (e.g. 'binance')."""

    def __init__(self, address_map: Mapping[str, str]) -> None:
        self._by_address: dict[str, str] = {
            k.lower(): v for k, v in address_map.items()
        }

    def __len__(self) -> int:
        return len(self._by_address)

    def classify(self, from_address: str | None) -> str | None:
        if not from_address:
            return None
        return self._by_address.get(from_address.lower())

    def sources(self) -> set[str]:
        return set(self._by_address.values())

    @classmethod
    def load(cls, path: Path | str) -> FundingSources:
        path = Path(path)
        if not path.exists():
            log.warning("funding-sources file missing: %s", path)
            return cls({})
        with path.open("r", encoding="utf-8") as f:
            raw: Any = yaml.safe_load(f)
        return cls.from_yaml(raw)

    @classmethod
    def from_yaml(cls, raw: Any) -> FundingSources:
        address_map: dict[str, str] = {}
        if isinstance(raw, Mapping):
            sources_raw = raw.get("sources") or {}
            if isinstance(sources_raw, Mapping):
                for source_name, addrs in sources_raw.items():
                    if not isinstance(addrs, list):
                        continue
                    for a in addrs:
                        if isinstance(a, str) and a.strip():
                            address_map[a.strip().lower()] = str(source_name)
        return cls(address_map)
