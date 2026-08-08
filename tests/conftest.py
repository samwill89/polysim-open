"""Shared pytest fixtures for PolySim test suite.

Phase 0: minimal — a tmp DB fixture that runs migrations.
Extended per phase: recorded HTTP fixtures (Phase 1), synthetic wallets
(Phase 2), fill-model edge cases (Phase 4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from polysim.db.migrations.runner import apply_migrations


@pytest.fixture
async def tmp_db(tmp_path: Path) -> AsyncIterator[Path]:
    """Fresh SQLite DB with all migrations applied."""
    db_path = tmp_path / "test.db"
    await apply_migrations(db_path)
    yield db_path
