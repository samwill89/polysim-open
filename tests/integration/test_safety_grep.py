"""CI safety grep behaviour tests. Spec §4 / §14 #1."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_safety_grep_clean_on_current_source() -> None:
    """The repo as-written must pass the safety grep."""
    script = _repo_root() / "scripts" / "ci_safety.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"safety grep unexpectedly failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_safety_grep_rejects_synthetic_violation(tmp_path: Path) -> None:
    """A file containing place_order() under src/ must fail the grep."""
    root = _repo_root()
    shutil.copytree(root / "src", tmp_path / "src")
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts" / "ci_safety.py", tmp_path / "scripts" / "ci_safety.py")
    shutil.copy(root / "pyproject.toml", tmp_path / "pyproject.toml")
    if (root / "config.example.yml").exists():
        shutil.copy(root / "config.example.yml", tmp_path / "config.example.yml")

    violator = tmp_path / "src" / "polysim" / "_bad.py"
    violator.write_text('def evil() -> None:\n    place_order("DO_NOT")\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "scripts/ci_safety.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (
        f"expected safety grep to reject place_order(), stdout:\n{result.stdout}"
    )
    assert "place_order" in result.stdout


def test_safety_grep_allows_fixtures_directory(tmp_path: Path) -> None:
    """tests/fixtures/ is explicitly allowed to reference these strings."""
    root = _repo_root()
    shutil.copytree(root / "src", tmp_path / "src")
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts" / "ci_safety.py", tmp_path / "scripts" / "ci_safety.py")
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "bad.py").write_text(
        "# fixture: place_order() must not trip the scanner here\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "scripts/ci_safety.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
