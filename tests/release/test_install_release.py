from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from coderus.release_install import (
    install_release_archive,
    validate_verification,
    write_verification,
)
from coderus.release_manifest import create_release_archive
from tests.test_release_manifest import make_source_tree


def test_install_release_validates_and_extracts_to_immutable_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
    )

    installed = install_release_archive(archive, tmp_path / "root")

    assert (installed / "coderus/app.py").read_text(encoding="utf-8") == (
        "print('coderus')\n"
    )
    assert (installed / "release.json").is_file()
    with pytest.raises(FileExistsError):
        install_release_archive(archive, tmp_path / "root")


def test_install_release_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.tar.gz"
    manifest = {
        "release_id": "20260721-120000-a1b2c3d4",
        "source_sha256": "a" * 64,
        "uv_lock_sha256": "b" * 64,
        "files": [],
    }
    with tarfile.open(archive, "w:gz") as bundle:
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("release.json")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
        escaped = tarfile.TarInfo("../escaped")
        escaped.size = 1
        bundle.addfile(escaped, io.BytesIO(b"x"))

    with pytest.raises(ValueError, match="unsafe archive path"):
        install_release_archive(archive, tmp_path / "root")
    assert (tmp_path / "escaped").exists() is False


def test_install_release_rejects_content_digest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
    )
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as original, tarfile.open(tampered, "w:gz") as output:
        for member in original.getmembers():
            content = original.extractfile(member).read()
            if member.name == "coderus/app.py":
                content = b"X" + content[1:]
            output.addfile(member, io.BytesIO(content))

    with pytest.raises(ValueError, match="digest mismatch"):
        install_release_archive(tampered, tmp_path / "root")
    assert not (tmp_path / "root/releases/20260721-120000-").exists()


def test_verification_binds_source_and_environment_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
    )
    release = install_release_archive(archive, tmp_path / "root")
    environment_file = release / ".venv/lib/package.py"
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text("version = 1\n", encoding="utf-8")

    write_verification(release, verified_at="2026-07-21T12:30:00Z")
    validate_verification(release)

    environment_file.write_text("version = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="environment changed after verification"):
        validate_verification(release)


def test_verification_rejects_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
    )
    release = install_release_archive(archive, tmp_path / "root")
    (release / ".venv").mkdir()
    write_verification(release, verified_at="2026-07-21T12:30:00Z")

    (release / "coderus/app.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source changed after verification"):
        validate_verification(release)
