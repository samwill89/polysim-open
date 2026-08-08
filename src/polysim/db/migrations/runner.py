"""SQLite migration runner — applies numbered .sql files idempotently.

Each migration file is `NNNN_name.sql`; version numbers must be strictly
increasing. A `_migrations` bookkeeping table records applied versions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

MIGRATIONS_DIR = Path(__file__).parent
_MIGRATION_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")


async def current_version(db_path: Path) -> int:
    """Return highest applied migration version, or 0 if none / DB missing."""
    if not db_path.exists():
        return 0
    async with aiosqlite.connect(str(db_path)) as db:
        try:
            async with db.execute(
                "SELECT COALESCE(MAX(version), 0) FROM _migrations"
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except aiosqlite.OperationalError:
            return 0


async def apply_migrations(db_path: Path) -> list[str]:
    """Apply any pending migrations in order. Returns names applied."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []

    files = sorted(
        p for p in MIGRATIONS_DIR.iterdir() if _MIGRATION_RE.match(p.name)
    )

    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

        async with db.execute("SELECT version FROM _migrations") as cur:
            done: set[int] = {int(r[0]) for r in await cur.fetchall()}

        for f in files:
            m = _MIGRATION_RE.match(f.name)
            if m is None:
                continue
            version = int(m.group(1))
            name = m.group(2)
            if version in done:
                continue

            sql = f.read_text(encoding="utf-8")
            await db.executescript(sql)
            await db.execute(
                "INSERT INTO _migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(UTC).isoformat()),
            )
            await db.commit()
            applied.append(f.name)

    return applied
