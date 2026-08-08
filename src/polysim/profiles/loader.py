"""RiskProfile YAML loader — addendum §3.

Profiles live in two locations, searched in order:
  1. Built-in, shipped with PolySim: src/polysim/profiles/*.yml
  2. User overrides: ~/.polysim/profiles/*.yml (takes precedence on name clash)

A profile is returned as a validated `RiskProfile`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from polysim.config import RiskProfile

BUILTIN_PROFILE_NAMES: tuple[str, ...] = ("systematic", "medium", "degen")


def builtin_dir() -> Path:
    return Path(__file__).parent


def user_dir() -> Path:
    return Path.home() / ".polysim" / "profiles"


def _search_dirs() -> list[Path]:
    # User dir searched first so operators can override a built-in by name.
    return [user_dir(), builtin_dir()]


def list_profiles() -> list[str]:
    """Names of all discoverable profiles (built-in + user), deduped by name."""
    seen: set[str] = set()
    out: list[str] = []
    for d in _search_dirs():
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yml")):
            name = f.stem
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


def profile_path(name: str) -> Path:
    for d in _search_dirs():
        candidate = d / f"{name}.yml"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Profile '{name}' not found. Known profiles: {list_profiles()}"
    )


def load_profile(name: str) -> RiskProfile:
    """Load and validate a profile by name."""
    path = profile_path(name)
    with path.open("r", encoding="utf-8") as f:
        raw: Any = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Profile {path} is not a YAML mapping")
    # Name in file must match filename so `profile show X` is unambiguous.
    raw.setdefault("name", name)
    if raw.get("name") != name:
        raise ValueError(
            f"Profile file {path} has name='{raw.get('name')}' but is named '{name}.yml'"
        )
    profile = RiskProfile.model_validate(raw)
    profile.ensure_sizing_fields()
    return profile


def load_all_builtin() -> list[RiskProfile]:
    return [load_profile(n) for n in BUILTIN_PROFILE_NAMES]
