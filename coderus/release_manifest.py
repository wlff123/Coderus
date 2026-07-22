from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import platform
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from coderus.public_release import require_public_files
from coderus.release_bootstrap import signed_manifest_payload

INCLUDED_DIRECTORIES = ("coderus", "tests", "scripts")
INCLUDED_FILES = (
    ".github/workflows/ci.yml",
    "docs/deployment.md",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "config.example.yaml",
)
EXCLUDED_PARTS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_files(root: Path) -> list[tuple[PurePosixPath, Path]]:
    candidates: list[Path] = []
    for directory in INCLUDED_DIRECTORIES:
        base = root / directory
        if base.is_dir():
            candidates.extend(path for path in base.rglob("*") if path.is_file())
    candidates.extend(path for name in INCLUDED_FILES if (path := root / name).is_file())

    files: list[tuple[PurePosixPath, Path]] = []
    for path in candidates:
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if path.is_symlink():
            raise ValueError(f"release files must not be symlinks: {relative}")
        if EXCLUDED_PARTS.intersection(relative.parts) or path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append((relative, path))
    files.sort(key=lambda item: item[0].as_posix())
    require_public_files(root, (path for _, path in files))
    return files


def build_source_manifest(root: Path) -> dict[str, object]:
    root = root.resolve()
    files = [
        {"path": relative.as_posix(), "sha256": _sha256(path), "size": path.stat().st_size}
        for relative, path in _release_files(root)
    ]
    digest = hashlib.sha256()
    for entry in files:
        digest.update(str(entry["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["sha256"]).encode("ascii"))
        digest.update(b"\0")
    return {"source_sha256": digest.hexdigest(), "files": files}


def _parse_created_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _load_signing_key(path: Path | None) -> Ed25519PrivateKey:
    if path is None:
        raise ValueError("release signing key is required")
    try:
        key = load_pem_private_key(path.expanduser().read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("invalid release signing key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("release signing key must be Ed25519")
    return key


def create_release_archive(
    root: Path,
    output_directory: Path,
    *,
    created_at: str | None = None,
    python_version: str | None = None,
    signing_key_path: Path | None = None,
) -> Path:
    root = root.resolve()
    source = build_source_manifest(root)
    created = _parse_created_at(created_at) if created_at else datetime.now(UTC)
    release_id = f"{created:%Y%m%d-%H%M%S}-{source['source_sha256'][:8]}"
    manifest = {
        "release_id": release_id,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "source_sha256": source["source_sha256"],
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "python_version": python_version or platform.python_version(),
        "schema_version": SCHEMA_VERSION,
        "min_schema_version": SCHEMA_VERSION,
        "max_schema_version": SCHEMA_VERSION,
        "signature_algorithm": "ed25519",
        "files": source["files"],
    }
    signing_key = _load_signing_key(signing_key_path)
    manifest["signature"] = base64.b64encode(
        signing_key.sign(signed_manifest_payload(manifest))
    ).decode("ascii")
    output_directory.mkdir(parents=True, exist_ok=True)
    destination = output_directory / f"coderus-{release_id}.tar.gz"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")

    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for relative, path in _release_files(root):
                info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                if relative.parts[0] == "scripts" and relative.suffix == ".sh":
                    info.mode = 0o755
                with path.open("rb") as source_file:
                    archive.addfile(info, source_file)
            info = tarfile.TarInfo("release.json")
            info.size = len(manifest_bytes)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(manifest_bytes))
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a verified Coderus release archive")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("dist/releases"))
    parser.add_argument("--signing-key", type=Path, required=True)
    args = parser.parse_args()
    archive = create_release_archive(
        args.root,
        args.output,
        signing_key_path=args.signing_key,
    )
    print(archive)


if __name__ == "__main__":
    main()
