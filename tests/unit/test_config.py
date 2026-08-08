"""Config-loader tests. Phase 0."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from polysim.config import Config, load_config


def _base_config() -> dict[str, object]:
    return {
        "run": {"name": "t", "mode": "backtest", "starting_balance_cents": 1000000},
        "categories": {
            "ai": {"enabled": True, "tier": "primary"},
        },
    }


def test_loads_minimal_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(_base_config()), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert isinstance(cfg, Config)
    assert cfg.run.name == "t"
    assert cfg.run.mode == "backtest"


def test_rejects_private_key(tmp_path: Path) -> None:
    raw = _base_config()
    raw["run"] = {**raw["run"], "private_key": "0xdeadbeef"}  # type: ignore[dict-item]
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="private_key"):
        load_config(cfg_path)


def test_rejects_nested_private_key(tmp_path: Path) -> None:
    raw = _base_config()
    raw["secrets"] = {"wallet_private_key": "0xabc"}
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="private_key"):
        load_config(cfg_path)


def test_rejects_unknown_mode(tmp_path: Path) -> None:
    raw = _base_config()
    raw["run"] = {**raw["run"], "mode": "live_trading"}  # type: ignore[dict-item]
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError or ValueError
        load_config(cfg_path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yml")


def test_signals_config_defaults_off(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(_base_config()), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.signals.enabled is False
    assert cfg.signals.provider == "reddit"
    assert cfg.signals.category_subreddits == {}


def test_signals_config_parses_overrides(tmp_path: Path) -> None:
    raw = _base_config()
    raw["signals"] = {
        "enabled": True,
        "provider": "fixture",
        "fixtures_dir": "tests/fixtures/signals",
        "category_subreddits": {"box_office": ["boxoffice", "movies"]},
    }
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.signals.enabled is True
    assert cfg.signals.provider == "fixture"
    assert cfg.signals.category_subreddits["box_office"] == ["boxoffice", "movies"]


def test_evidence_config_defaults_off(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(_base_config()), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.evidence.enabled is False
    assert cfg.evidence.provider == "gdelt"
    assert cfg.evidence.max_age_hours == 6.0


def test_evidence_config_parses_overrides(tmp_path: Path) -> None:
    raw = _base_config()
    raw["evidence"] = {
        "enabled": True,
        "provider": "none",
        "lookback_days": 3,
        "max_articles": 12,
    }
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.evidence.enabled is True
    assert cfg.evidence.provider == "none"
    assert cfg.evidence.lookback_days == 3
    assert cfg.evidence.max_articles == 12


def test_evidence_config_rejects_unbounded_scan_values(tmp_path: Path) -> None:
    raw = _base_config()
    raw["evidence"] = {
        "enabled": True,
        "lookback_days": 0,
        "max_articles": 500,
    }
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_path)
