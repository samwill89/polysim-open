"""Sync warproxxx/poly_data — empirical-priors addendum §3.2 + §11 Q2.

Clones (or pulls) the public poly_data repo into ~/.polysim/poly_data/.
Staleness check: if latest trade timestamp > 24h old, log a warning and
let the caller decide whether to fall back to direct CTF event log
scraping via Alchemy.

The repo URL defaults to the canonical one but is overridable for forks.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_REPO_URL = "https://github.com/warproxxx/poly_data.git"
DEFAULT_LOCAL_DIR = Path.home() / ".polysim" / "poly_data"
STALENESS_THRESHOLD = timedelta(hours=24)


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a sync attempt."""

    repo_dir: Path
    fetched: bool                          # was a network operation performed?
    latest_trade_ts: datetime | None       # newest trade observed in the repo
    is_stale: bool                         # latest_trade_ts older than threshold


def _have_git() -> bool:
    return shutil.which("git") is not None


async def sync_poly_data(
    *,
    repo_url: str = DEFAULT_REPO_URL,
    local_dir: Path = DEFAULT_LOCAL_DIR,
    timeout_s: float = 120.0,
) -> SyncResult:
    """Clone or pull the poly_data repo. Returns SyncResult.

    Network-bound; safe to call from async code via run_in_executor for
    the git subprocess. Errors do not raise — they return `fetched=False`
    so the caller can fall back to direct CTF scraping.
    """
    local_dir = Path(local_dir)
    local_dir.parent.mkdir(parents=True, exist_ok=True)

    if not _have_git():
        log.warning("poly_data sync: git binary not found on PATH; skipping")
        return SyncResult(
            repo_dir=local_dir, fetched=False,
            latest_trade_ts=None, is_stale=True,
        )

    fetched = False
    try:
        if (local_dir / ".git").exists():
            await _run_git(["pull", "--ff-only"], cwd=local_dir, timeout=timeout_s)
        else:
            await _run_git(
                ["clone", "--depth", "1", repo_url, str(local_dir)],
                cwd=None, timeout=timeout_s,
            )
        fetched = True
    except (subprocess.CalledProcessError, TimeoutError) as exc:
        log.warning("poly_data sync failed: %s", exc)

    latest = await _detect_latest_trade_ts(local_dir)
    is_stale = latest is None or (datetime.now(UTC) - latest) > STALENESS_THRESHOLD

    if is_stale:
        log.warning(
            "poly_data is stale (latest=%s, threshold=%s); "
            "operator should consider falling back to CTF scraper",
            latest, STALENESS_THRESHOLD,
        )

    return SyncResult(
        repo_dir=local_dir, fetched=fetched,
        latest_trade_ts=latest, is_stale=is_stale,
    )


async def _run_git(
    args: list[str], *, cwd: Path | None, timeout: float  # noqa: ASYNC109 — wrap subprocess timeout
) -> None:
    """Invoke git in a subprocess; raise CalledProcessError on non-zero."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode or 1, ["git", *args])


async def _detect_latest_trade_ts(repo_dir: Path) -> datetime | None:
    """Best-effort: infer latest trade ts from any parquet/CSV/jsonl in repo.

    poly_data's structure isn't formalized; we look for the newest
    *.parquet / *.csv / *.jsonl file mtime and return that as a proxy.
    Real schemas: callers using `features.py` will read directly.
    """
    if not repo_dir.exists():
        return None
    candidates: list[Path] = []
    for ext in ("*.parquet", "*.csv", "*.jsonl"):
        candidates.extend(repo_dir.rglob(ext))
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC)


__all__ = [
    "DEFAULT_LOCAL_DIR",
    "DEFAULT_REPO_URL",
    "STALENESS_THRESHOLD",
    "SyncResult",
    "sync_poly_data",
]
