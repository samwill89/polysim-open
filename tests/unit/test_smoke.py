"""Smoke tests — every public module must import. Phase 0 acceptance."""

from __future__ import annotations

import importlib

import pytest

MODULES = [
    "polysim",
    "polysim.cli",
    "polysim.config",
    "polysim.models",
    "polysim.db.dao",
    "polysim.db.migrations.runner",
    "polysim.ingest.polymarket_ws",
    "polysim.ingest.polymarket_rest",
    "polysim.ingest.polygon_rpc",
    "polysim.profiler.wallet_profiler",
    "polysim.scoring.base",
    "polysim.scoring.category_insider",
    "polysim.scoring.event_insider",
    "polysim.scoring.fresh_wallet",
    "polysim.scoring.coordination",
    "polysim.scoring.timing",
    "polysim.scoring.composite",
    "polysim.investigator.agent",
    "polysim.paper.fill_model",
    "polysim.paper.portfolio",
    "polysim.paper.executor",
    "polysim.evaluator.metrics",
    "polysim.evaluator.backtest",
    "polysim.reporter.cli_dashboard",
    "polysim.reporter.telegram",
    "polysim.utils.logging",
    "polysim.utils.time",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    importlib.import_module(module)


def test_version_is_set() -> None:
    import polysim

    assert polysim.__version__
