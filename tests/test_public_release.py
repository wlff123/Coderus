from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCANNER = ROOT / "scripts" / "check-public-release.py"


def run_scan(root: Path, *, all_files: bool = True) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCANNER), "--root", str(root)]
    if all_files:
        command.append("--all-files")
    return subprocess.run(command, capture_output=True, text=True)


def test_public_release_scanner_accepts_safe_source_tree(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    package = tmp_path / "package"
    package.mkdir()
    (package / "app.py").write_text("print('hello')\n", encoding="utf-8")

    result = run_scan(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Public release scan passed" in result.stdout


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("secrets.env", "EXAMPLE=value\n"),
        ("private.txt", "-----BEGIN " + "PRIVATE KEY-----\n"),
        ("token.txt", "gh" + "p_" + "a" * 36),
        ("notes.md", "C:\\Users\\alice\\project\n"),
        ("database.db", "not a database"),
    ],
)
def test_public_release_scanner_rejects_sensitive_content_and_files(
    tmp_path: Path, relative_path: str, content: str
) -> None:
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    result = run_scan(tmp_path)

    assert result.returncode == 1
    assert relative_path in result.stdout


def test_repository_candidate_passes_public_release_scan() -> None:
    result = run_scan(ROOT, all_files=False)

    assert result.returncode == 0, result.stdout + result.stderr
