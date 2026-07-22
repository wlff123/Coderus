from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from coderus.release_install import (
    install_release_archive,
    validate_verification,
    write_verification,
)
from coderus.release_manifest import create_release_archive
from tests.test_release_manifest import make_source_tree

ROOT = Path(__file__).parents[2]


def release_keys(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_path = root / "private.pem"
    public_path = root / "public.pem"
    private_path.write_bytes(
        private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    return private_path, public_path


def test_install_release_validates_and_extracts_to_immutable_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    private_key, public_key = release_keys(tmp_path / "keys")
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
        signing_key_path=private_key,
    )

    installed = install_release_archive(archive, tmp_path / "root", public_key_path=public_key)

    assert (installed / "coderus/app.py").read_text(encoding="utf-8") == (
        "print('coderus')\n"
    )
    assert (installed / "release.json").is_file()
    with pytest.raises(FileExistsError):
        install_release_archive(archive, tmp_path / "root", public_key_path=public_key)


def test_install_release_rejects_path_traversal(tmp_path: Path) -> None:
    _, public_key = release_keys(tmp_path / "keys")
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
        install_release_archive(archive, tmp_path / "root", public_key_path=public_key)
    assert (tmp_path / "escaped").exists() is False


def test_install_release_rejects_content_digest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    private_key, public_key = release_keys(tmp_path / "keys")
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
        signing_key_path=private_key,
    )
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as original, tarfile.open(tampered, "w:gz") as output:
        for member in original.getmembers():
            content = original.extractfile(member).read()
            if member.name == "coderus/app.py":
                content = b"X" + content[1:]
            output.addfile(member, io.BytesIO(content))

    with pytest.raises(ValueError, match="digest mismatch"):
        install_release_archive(tampered, tmp_path / "root", public_key_path=public_key)
    assert not (tmp_path / "root/releases/20260721-120000-").exists()


def test_verification_binds_source_and_environment_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    private_key, public_key = release_keys(tmp_path / "keys")
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
        signing_key_path=private_key,
    )
    release = install_release_archive(archive, tmp_path / "root", public_key_path=public_key)
    environment_file = release / ".venv/lib/package.py"
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text("version = 1\n", encoding="utf-8")

    write_verification(
        release,
        verified_at="2026-07-21T12:30:00Z",
        public_key_path=public_key,
    )
    validate_verification(release, public_key_path=public_key)

    environment_file.write_text("version = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="environment changed after verification"):
        validate_verification(release, public_key_path=public_key)


def test_verification_rejects_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    private_key, public_key = release_keys(tmp_path / "keys")
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
        signing_key_path=private_key,
    )
    release = install_release_archive(archive, tmp_path / "root", public_key_path=public_key)
    (release / ".venv").mkdir()
    write_verification(
        release,
        verified_at="2026-07-21T12:30:00Z",
        public_key_path=public_key,
    )

    (release / "coderus/app.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source changed after verification"):
        validate_verification(release, public_key_path=public_key)


def test_install_rejects_manifest_signed_by_an_untrusted_key(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    private_key, _ = release_keys(tmp_path / "signer")
    _, untrusted_public_key = release_keys(tmp_path / "verifier")
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        signing_key_path=private_key,
    )

    with pytest.raises(ValueError, match="signature"):
        install_release_archive(
            archive,
            tmp_path / "root",
            public_key_path=untrusted_public_key,
        )


def test_standalone_bootstrap_does_not_import_deployed_coderus(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_source_tree(source)
    private_key, public_key = release_keys(tmp_path / "keys")
    archive = create_release_archive(
        source,
        tmp_path / "incoming",
        created_at="2026-07-21T12:00:00Z",
        signing_key_path=private_key,
    )
    old_environment = tmp_path / "old-environment"
    old_package = old_environment / "coderus"
    old_package.mkdir(parents=True)
    (old_package / "__init__.py").write_text("", encoding="utf-8")
    (old_package / "release_install.py").write_text(
        "raise RuntimeError('old installer was imported')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "coderus" / "release_bootstrap.py"),
            str(archive),
            "--root",
            str(tmp_path / "root"),
            "--public-key",
            str(public_key),
        ],
        env={**os.environ, "PYTHONPATH": str(old_environment)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    release = Path(result.stdout.strip())
    assert (release / "coderus/app.py").read_text(encoding="utf-8") == (
        "print('coderus')\n"
    )
