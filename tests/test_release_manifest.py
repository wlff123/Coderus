from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from coderus.release_manifest import (
    build_source_manifest,
    create_release_archive,
)


def make_source_tree(root: Path) -> None:
    files = {
        "coderus/app.py": "print('coderus')\n",
        "coderus/__pycache__/app.pyc": "cache",
        "tests/test_app.py": "def test_app(): pass\n",
        "scripts/start.sh": "#!/bin/sh\n",
        "pyproject.toml": "[project]\nname='coderus'\n",
        "uv.lock": "version = 1\n",
        "README.md": "# Coderus\n",
        "LICENSE": "Apache License 2.0\n",
        "config.example.yaml": "server: {}\n",
        "config.yaml": "secret: production\n",
        "secrets.env": "CODERUS_MODEL_API_KEY=secret\n",
        "data/coderus.db": "database",
        ".git/config": "git",
        ".venv/pyvenv.cfg": "venv",
        "docs/internal.md": "not part of runtime release",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_source_manifest_uses_runtime_whitelist_and_stable_digest(tmp_path: Path) -> None:
    make_source_tree(tmp_path)

    first = build_source_manifest(tmp_path)
    (tmp_path / "data/coderus.db").write_text("changed", encoding="utf-8")
    second = build_source_manifest(tmp_path)

    paths = [entry["path"] for entry in first["files"]]
    assert paths == sorted(paths)
    assert "coderus/app.py" in paths
    assert "tests/test_app.py" in paths
    assert "LICENSE" in paths
    assert "config.yaml" not in paths
    assert "secrets.env" not in paths
    assert "data/coderus.db" not in paths
    assert "coderus/__pycache__/app.pyc" not in paths
    assert first["source_sha256"] == second["source_sha256"]


def test_create_release_archive_contains_manifest_and_no_runtime_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "dist"
    make_source_tree(source)

    archive_path = create_release_archive(
        source,
        output,
        created_at="2026-07-21T12:00:00Z",
        python_version="3.12.11",
    )

    assert archive_path.name.startswith("coderus-20260721-120000-")
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
        release = json.load(archive.extractfile("release.json"))
        script_mode = archive.getmember("scripts/start.sh").mode

    assert "release.json" in names
    assert "coderus/app.py" in names
    assert "LICENSE" in names
    assert "config.yaml" not in names
    assert "secrets.env" not in names
    assert "data/coderus.db" not in names
    assert release["release_id"] in archive_path.name
    assert release["python_version"] == "3.12.11"
    assert script_mode == 0o755
    assert "tests" not in release


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("coderus/.env", "TOKEN=value\n"),
        ("scripts/deploy-key.pem", "-----BEGIN " + "PRIVATE KEY-----\n"),
    ],
)
def test_source_manifest_rejects_sensitive_files_inside_release_directories(
    tmp_path: Path, relative_path: str, content: str
) -> None:
    make_source_tree(tmp_path)
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="public release rejected"):
        build_source_manifest(tmp_path)
