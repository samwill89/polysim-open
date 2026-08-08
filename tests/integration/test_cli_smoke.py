"""CLI smoke — `polysim init` and `polysim status` work on a fresh dir."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from polysim.cli import app


def test_version_prints() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "polysim" in result.stdout.lower()


def test_init_creates_db_and_config(tmp_path: Path, monkeypatch: object) -> None:
    # Copy templates so the CLI's template-locator finds them.
    repo_root = Path(__file__).resolve().parents[2]
    (tmp_path / "config.example.yml").write_bytes(
        (repo_root / "config.example.yml").read_bytes()
    )
    (tmp_path / ".env.example").write_bytes((repo_root / ".env.example").read_bytes())

    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["init", "--db", "test.db", "--config", "config.yml"])
        assert result.exit_code == 0, result.stdout
        assert (tmp_path / "test.db").exists()
        assert (tmp_path / "config.yml").exists()
    finally:
        os.chdir(cwd)


def test_status_on_uninitialized_dir(tmp_path: Path) -> None:
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["status", "--db", "does-not-exist.db", "--config", "nope.yml"],
        )
        assert result.exit_code == 0  # status tolerates missing state
        assert "not initialized" in result.stdout or "missing" in result.stdout
    finally:
        os.chdir(cwd)
    # keep shutil used — avoid unused-import lint
    _ = shutil
