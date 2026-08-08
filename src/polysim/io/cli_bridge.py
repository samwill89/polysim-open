"""Read-only polymarket-cli wrapper — empirical-priors addendum §7.2.

A sanctioned bridge for ad-hoc agent use during development. Production
trading uses py-clob-client directly via PolymarketREST.

Safety:
  * The only legal subcommands are in ALLOWED_SUBCOMMANDS below.
  * Any attempt to run an order-placement subcommand is rejected before
    spawning a subprocess, with a clear error.
  * The CI grep (scripts/ci_safety.py) bans 'polymarket.*order' and
    'clob.*place' literals in this file specifically.
  * Errors are returned, never raised, so callers can route around CLI
    flakiness without crashing the process.

Karpathy's framing (§7): treat the CLI as agent-native tooling — but
hand it to the agent through a safe contract, not raw subprocess.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from typing import Final

log = logging.getLogger(__name__)


# Allow-list. Adding entries requires both the addition here and the
# corresponding CI-grep update — that's the deliberate friction.
ALLOWED_SUBCOMMANDS: Final[frozenset[tuple[str, ...]]] = frozenset({
    ("markets", "list"),
    ("markets", "show"),
    ("clob", "book"),
    ("clob", "midpoint"),
    ("clob", "spread"),
})


# Anything starting with these prefixes is hard-rejected even if a future
# typo accidentally allow-listed it. Defense in depth — the CI grep is
# layer 1, this list is layer 2.
DENY_TOKENS: Final[tuple[str, ...]] = (
    "place", "submit", "execute", "trade", "order",
    "cancel", "sell", "buy",
)


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI call."""

    ok: bool
    stdout: str
    stderr: str
    returncode: int
    skipped_reason: str | None = None


def _have_cli() -> bool:
    return shutil.which("polymarket-cli") is not None


def _is_allowed(parts: tuple[str, ...]) -> tuple[bool, str | None]:
    """Allow-list + deny-token gate."""
    if not parts:
        return False, "empty subcommand"
    for tok in parts:
        low = tok.lower()
        for deny in DENY_TOKENS:
            if deny in low:
                return False, f"denied token in arg: {tok!r}"
    # Match against allow-list (prefix-style: ('markets', 'list') matches
    # ('markets', 'list', '--limit', '5')).
    for allowed in ALLOWED_SUBCOMMANDS:
        if len(parts) >= len(allowed) and parts[: len(allowed)] == allowed:
            return True, None
    return False, f"not on allow-list: {parts}"


async def run(
    *args: str,
    timeout_s: float = 30.0,
) -> CliResult:
    """Invoke `polymarket-cli <args...>` after gating against the allow-list.

    Returns a CliResult; errors are NOT raised. Stdout is captured and
    returned verbatim for the caller to parse.
    """
    parts = tuple(args)
    allowed, reason = _is_allowed(parts)
    if not allowed:
        log.warning("cli_bridge rejected: %s", reason)
        return CliResult(
            ok=False, stdout="", stderr="",
            returncode=2, skipped_reason=reason,
        )
    if not _have_cli():
        return CliResult(
            ok=False, stdout="", stderr="",
            returncode=127,
            skipped_reason="polymarket-cli not on PATH",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            "polymarket-cli", *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
            )
        except TimeoutError:
            proc.kill()
            return CliResult(
                ok=False, stdout="", stderr="timeout",
                returncode=124, skipped_reason="cli timed out",
            )
    except (OSError, asyncio.CancelledError) as exc:
        return CliResult(
            ok=False, stdout="", stderr=repr(exc),
            returncode=1, skipped_reason=type(exc).__name__,
        )
    return CliResult(
        ok=(proc.returncode == 0),
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        returncode=proc.returncode or 0,
    )


__all__ = [
    "ALLOWED_SUBCOMMANDS",
    "DENY_TOKENS",
    "CliResult",
    "run",
]
