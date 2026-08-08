"""Funding-source heuristic tests."""

from __future__ import annotations

from pathlib import Path

from polysim.ingest.funding_sources import FundingSources


def test_classify_exact_match() -> None:
    fs = FundingSources({"0xAbC": "binance", "0xdef": "coinbase"})
    assert fs.classify("0xabc") == "binance"
    assert fs.classify("0XABC") == "binance"
    assert fs.classify("0xdef") == "coinbase"


def test_classify_unknown() -> None:
    fs = FundingSources({"0xabc": "binance"})
    assert fs.classify("0xdeadbeef") is None


def test_classify_none() -> None:
    fs = FundingSources({"0xabc": "binance"})
    assert fs.classify(None) is None
    assert fs.classify("") is None


def test_load_missing_file(tmp_path: Path) -> None:
    fs = FundingSources.load(tmp_path / "missing.yml")
    assert len(fs) == 0


def test_load_real_config() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "configs" / "funding_sources.yml"
    fs = FundingSources.load(path)
    assert len(fs) > 0
    assert "binance" in fs.sources()
    assert "coinbase" in fs.sources()


def test_from_yaml_parses_expected_shape() -> None:
    raw = {
        "sources": {
            "binance": ["0xAAA", "0xBBB"],
            "coinbase": ["0xCCC"],
        }
    }
    fs = FundingSources.from_yaml(raw)
    assert fs.classify("0xaaa") == "binance"
    assert fs.classify("0xccc") == "coinbase"


def test_from_yaml_ignores_garbage() -> None:
    raw = {"sources": "not-a-dict"}
    fs = FundingSources.from_yaml(raw)
    assert len(fs) == 0
