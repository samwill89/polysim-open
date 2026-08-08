"""PolySim safety grep — enforce spec §4 and §14 #1.

Fails the build (exit 1) if any order-placement API identifier appears in
source code outside tests/fixtures/.  Cross-platform (Windows + Linux + mac).

Search scope — explicit list:
    src/, pyproject.toml, config.example.yml,
    scripts/seed_historical.py, scripts/replay.py (if present).
Excluded:  tests/fixtures/ (allowed per §14 #1),
           this script and scripts/ci_safety.sh (reference the strings).

Usage:
    python scripts/ci_safety.py      # exit 0 clean, 1 on violation
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN = re.compile(r"\b(place_order|create_order|cancel_order|post_order|sign_order)\b")

# Empirical-priors addendum §9.5: ban the mid_price Python identifier in
# valuation paths. English prose forms like "mid-price" or "midpoint" are
# fine — only the underscore form is banned, per the addendum's grep.
FORBIDDEN_MID_PRICE = re.compile(r"\bmid_price\b")

# CLI bridge must not contain any order-placement primitives. The `run()`
# function gates by allow-list at runtime; this grep is the second layer.
FORBIDDEN_CLI_ORDER = re.compile(
    r"polymarket-cli\s+(?:place|submit|execute|trade|order|cancel|buy|sell)",
    re.IGNORECASE,
)
CLI_BRIDGE_PATHS = [Path("src/polysim/io/cli_bridge.py")]
MID_PRICE_SCAN_PATHS = [
    Path("src/polysim/portfolio/valuation.py"),
]

SCAN_EXTENSIONS = {".py", ".sql", ".yml", ".yaml", ".toml", ".json"}


def _scan_file(path: Path) -> list[str]:
    violations: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                if FORBIDDEN.search(line):
                    violations.append(f"{path}:{lineno}:{line.rstrip()}")
    except OSError:
        # File vanished between listing and opening — ignore.
        pass
    return violations


def _scan_path(path: Path) -> list[str]:
    if not path.exists():
        return []
    if path.is_file():
        # Scan pyproject.toml / config.example.yml regardless of extension set membership.
        if path.suffix in SCAN_EXTENSIONS or path.name in {"pyproject.toml", "config.example.yml"}:
            return _scan_file(path)
        return []
    violations: list[str] = []
    for p in sorted(path.rglob("*")):
        if p.is_file() and p.suffix in SCAN_EXTENSIONS:
            violations.extend(_scan_file(p))
    return violations


def main() -> int:
    root = Path.cwd()
    scan_paths = [
        root / "src",
        root / "pyproject.toml",
        root / "config.example.yml",
        root / "scripts" / "seed_historical.py",
        root / "scripts" / "replay.py",
    ]

    violations: list[str] = []
    for p in scan_paths:
        violations.extend(_scan_path(p))

    # Mid-price ban in valuation paths (addendum §4.1 / §9.5).
    mid_violations: list[str] = []
    for rel in MID_PRICE_SCAN_PATHS:
        p = root / rel
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    # Allow the word in comments that explicitly ban it
                    # (e.g. "mid-price MTM is forbidden here").
                    stripped = line.lstrip()
                    if stripped.startswith("#") or stripped.startswith('"""'):
                        continue
                    if FORBIDDEN_MID_PRICE.search(line):
                        mid_violations.append(f"{p}:{lineno}:{line.rstrip()}")
        except OSError:
            pass

    if violations:
        print("FAIL  Safety violation — forbidden order-placement API in source:")
        print()
        for v in violations:
            print(v)
        print()
        print(
            "Spec §4 / §14 #1 forbid these identifiers outside tests/fixtures/:"
        )
        print("  place_order, create_order, cancel_order, post_order, sign_order")
        return 1

    if mid_violations:
        print(
            "FAIL  Safety violation — mid_price used in valuation path "
            "(addendum §4.1 requires bid-price MTM):"
        )
        for v in mid_violations:
            print(v)
        return 1

    # CLI-bridge order-placement ban (addendum §7.2 / §9.5).
    cli_violations: list[str] = []
    for rel in CLI_BRIDGE_PATHS:
        p = root / rel
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if FORBIDDEN_CLI_ORDER.search(line):
                        cli_violations.append(f"{p}:{lineno}:{line.rstrip()}")
        except OSError:
            pass
    if cli_violations:
        print(
            "FAIL  Safety violation — order-placement CLI invocation in "
            "cli_bridge (addendum §7.2 forbids):"
        )
        for v in cli_violations:
            print(v)
        return 1

    print("OK  Safety grep clean — no forbidden order-placement APIs in source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
