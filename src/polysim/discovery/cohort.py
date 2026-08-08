"""Frozen-cohort selection — empirical-priors addendum §3.4 + §3.5.

Once an experiment begins, the cohort is frozen: no wallets added or
removed. This is the survivorship-bias guard. Cohort composition:

  Primary pool (niche-matched):
    - Top 100 AEC wallets by edge_likelihood_aec (≥ 50 niche trades)
    - Top 100 AI-labs wallets by edge_likelihood_ai_labs
    - Top 100 creator-econ wallets by edge_likelihood_creator_econ

  Secondary pool (general edge):
    - Top 200 by edge_likelihood_global, with primary-pool dedup

  Total: ~500. Stamped to `wallets_discovery.is_cohort = 1` and
  `experiments.cohort_hash` (SHA256 of sorted addresses) so cohort
  identity is verifiable post-hoc.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite

from polysim.discovery.classifier import ClassifierScore
from polysim.discovery.features import WalletFeatures
from polysim.discovery.niche_tags import (
    NICHE_TAGS_VERSION,
    primary_niches,
)
from polysim.utils.time import iso, now_utc

log = logging.getLogger(__name__)

DEFAULT_PER_NICHE_TARGET = 0      # Was 100; calibrated 2026-04-28 — niche pools were diluting (resolved-market analysis: general pool +42% ROI, niches ~0%).
DEFAULT_GENERAL_TARGET = 300      # Bumped from 200 to absorb the headroom.
DEFAULT_MIN_NICHE_TRADES = 50


@dataclass(frozen=True)
class CohortPick:
    wallet_address: str
    pool: str                            # 'aec' | 'ai_labs' | 'creator_econ' | 'general'
    edge_likelihood: float


def select_cohort(
    scores: Iterable[ClassifierScore],
    features_by_key: dict[tuple[str, str], WalletFeatures],
    *,
    per_niche_target: int = DEFAULT_PER_NICHE_TARGET,
    general_target: int = DEFAULT_GENERAL_TARGET,
    min_niche_trades: int = DEFAULT_MIN_NICHE_TRADES,
) -> list[CohortPick]:
    """Pure selection. Returns the cohort list in insertion order
    (primary pools first, then general)."""
    score_list = list(scores)
    by_scope: dict[str, list[ClassifierScore]] = defaultdict(list)
    for s in score_list:
        by_scope[s.scope].append(s)
    picks: list[CohortPick] = []
    primary_addresses: set[str] = set()
    # Primary pool — per niche, top-K with the trade-count gate.
    for niche in primary_niches():
        sorted_in_niche = sorted(
            by_scope.get(niche, []),
            key=lambda s: -s.edge_likelihood,
        )
        for s in sorted_in_niche:
            if len(picks) and (
                len([p for p in picks if p.pool == niche]) >= per_niche_target
            ):
                break
            f = features_by_key.get((s.wallet_address, niche))
            if f is None or f.trade_count < min_niche_trades:
                continue
            if s.wallet_address in primary_addresses:
                continue
            picks.append(CohortPick(
                wallet_address=s.wallet_address,
                pool=niche,
                edge_likelihood=s.edge_likelihood,
            ))
            primary_addresses.add(s.wallet_address)
    # Secondary pool — top-N global, dedup vs primary.
    sorted_global = sorted(
        by_scope.get("global", []),
        key=lambda s: -s.edge_likelihood,
    )
    general_count = 0
    for s in sorted_global:
        if general_count >= general_target:
            break
        if s.wallet_address in primary_addresses:
            continue
        picks.append(CohortPick(
            wallet_address=s.wallet_address,
            pool="general",
            edge_likelihood=s.edge_likelihood,
        ))
        general_count += 1
    return picks


def cohort_hash(picks: Iterable[CohortPick]) -> str:
    """SHA256 of sorted unique wallet addresses — verifies cohort identity."""
    addresses = sorted({p.wallet_address.lower() for p in picks})
    h = hashlib.sha256()
    for addr in addresses:
        h.update(addr.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


async def freeze_cohort(
    db_path: Path,
    *,
    experiment_name: str,
    picks: list[CohortPick],
    classifier_version: str,
    belief_schema_version: str,
    notes: str | None = None,
) -> int:
    """Persist cohort to wallets_discovery + create experiments row.

    Returns experiment_id. Idempotent on (experiment_name): re-running
    overwrites the cohort_hash + size + frozen_at.
    """
    started = now_utc()
    chash = cohort_hash(picks)
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            """
            INSERT INTO experiments(
                name, version, niche_tags_version, belief_schema_version,
                started_at, cohort_frozen_at, cohort_hash, cohort_size, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                cohort_frozen_at = excluded.cohort_frozen_at,
                cohort_hash = excluded.cohort_hash,
                cohort_size = excluded.cohort_size,
                niche_tags_version = excluded.niche_tags_version,
                belief_schema_version = excluded.belief_schema_version
            """,
            (
                experiment_name,
                f"{NICHE_TAGS_VERSION}+{classifier_version}+{belief_schema_version}",
                NICHE_TAGS_VERSION,
                belief_schema_version,
                iso(started),
                iso(started),
                chash,
                len(picks),
                notes,
            ),
        )
        await cur.close()
        # Look up the id (handles both insert and conflict-update path).
        async with db.execute(
            "SELECT id FROM experiments WHERE name = ?", (experiment_name,)
        ) as q:
            row = await q.fetchone()
        if row is None:
            raise RuntimeError(f"failed to upsert experiment {experiment_name}")
        experiment_id = int(row[0])

        # Reset is_cohort/cohort_niche on prior cohort entries for this experiment.
        await db.execute(
            "UPDATE wallets_discovery "
            "SET is_cohort = 0, cohort_niche = NULL, experiment_id = NULL "
            "WHERE experiment_id = ?",
            (experiment_id,),
        )
        # Stamp every pick.
        for p in picks:
            await db.execute(
                """
                INSERT INTO wallets_discovery(
                    address, first_seen_at, is_cohort, cohort_niche,
                    experiment_id, last_classified_at,
                    edge_likelihood_global, edge_likelihood_aec,
                    edge_likelihood_ai_labs, edge_likelihood_creator_econ
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    is_cohort = 1,
                    cohort_niche = excluded.cohort_niche,
                    experiment_id = excluded.experiment_id,
                    last_classified_at = excluded.last_classified_at
                """,
                (
                    p.wallet_address.lower(), iso(started),
                    p.pool, experiment_id, iso(started),
                    None, None, None, None,
                ),
            )
        await db.commit()
    log.info(
        "cohort frozen: experiment=%s id=%d size=%d hash=%s...",
        experiment_name, experiment_id, len(picks), chash[:12],
    )
    return experiment_id


async def update_edge_likelihoods(
    db_path: Path, scores: list[ClassifierScore]
) -> int:
    """Write edge_likelihood_{global,aec,ai_labs,creator_econ} per wallet.

    Idempotent: existing rows get UPDATE'd, missing addresses get INSERT'd
    with default first_seen_at = now.
    """
    if not scores:
        return 0
    by_addr: dict[str, dict[str, float]] = defaultdict(dict)
    for s in scores:
        by_addr[s.wallet_address.lower()][s.scope] = s.edge_likelihood

    now = iso(now_utc())
    written = 0
    async with aiosqlite.connect(str(db_path)) as db:
        for addr, scope_map in by_addr.items():
            await db.execute(
                """
                INSERT INTO wallets_discovery(
                    address, first_seen_at, last_classified_at,
                    edge_likelihood_global, edge_likelihood_aec,
                    edge_likelihood_ai_labs, edge_likelihood_creator_econ
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    last_classified_at = excluded.last_classified_at,
                    edge_likelihood_global = COALESCE(excluded.edge_likelihood_global, wallets_discovery.edge_likelihood_global),
                    edge_likelihood_aec = COALESCE(excluded.edge_likelihood_aec, wallets_discovery.edge_likelihood_aec),
                    edge_likelihood_ai_labs = COALESCE(excluded.edge_likelihood_ai_labs, wallets_discovery.edge_likelihood_ai_labs),
                    edge_likelihood_creator_econ = COALESCE(excluded.edge_likelihood_creator_econ, wallets_discovery.edge_likelihood_creator_econ)
                """,
                (
                    addr, now, now,
                    scope_map.get("global"),
                    scope_map.get("aec"),
                    scope_map.get("ai_labs"),
                    scope_map.get("creator_econ"),
                ),
            )
            written += 1
        await db.commit()
    return written


__all__ = [
    "DEFAULT_GENERAL_TARGET",
    "DEFAULT_MIN_NICHE_TRADES",
    "DEFAULT_PER_NICHE_TARGET",
    "CohortPick",
    "cohort_hash",
    "freeze_cohort",
    "select_cohort",
    "update_edge_likelihoods",
]


# Keep used-as-types imports referenced.
_ = datetime
