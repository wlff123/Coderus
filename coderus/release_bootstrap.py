from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

RELEASE_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024


def signed_manifest_payload(manifest: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _safe_member_name(name: str) -> PurePosixPath:
    if "\\" in name:
        raise ValueError(f"unsafe archive path: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe archive path: {name}")
    if path.parts[0].endswith(":"):
        raise ValueError(f"unsafe archive path: {name}")
    return path


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_digest(files: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(files, key=lambda item: str(item["path"])):
        digest.update(str(entry["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["sha256"]).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def verify_manifest_signature(
    manifest: dict[str, object], public_key_path: Path | None
) -> None:
    if public_key_path is None:
        raise ValueError("release public key is required")
    if manifest.get("signature_algorithm") != "ed25519":
        raise ValueError("unsupported release signature algorithm")
    signature = manifest.get("signature")
    if not isinstance(signature, str):
        raise ValueError("release signature is missing")
    try:
        key = load_pem_public_key(public_key_path.expanduser().read_bytes())
        signature_bytes = base64.b64decode(signature, validate=True)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("invalid release signature") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("release public key must be Ed25519")
    try:
        key.verify(signature_bytes, signed_manifest_payload(manifest))
    except InvalidSignature as exc:
        raise ValueError("invalid release signature") from exc


def install_release_archive(
    archive_path: Path,
    root: Path,
    *,
    public_key_path: Path | None = None,
) -> Path:
    root = root.expanduser().resolve()
    releases = root / "releases"
    if releases.is_symlink():
        raise ValueError("releases directory must not be a symlink")
    releases.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as archive:
        names: dict[str, tarfile.TarInfo] = {}
        declared_bytes = 0
        for member_index, member in enumerate(archive, start=1):
            if member_index > MAX_ARCHIVE_FILES:
                raise ValueError("release archive contains too many files")
            declared_bytes += member.size
            if declared_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError("release archive is too large")
            safe_name = _safe_member_name(member.name).as_posix()
            if not member.isfile():
                raise ValueError(f"archive member is not a regular file: {safe_name}")
            if safe_name in names:
                raise ValueError(f"duplicate archive member: {safe_name}")
            names[safe_name] = member

        release_member = names.get("release.json")
        if release_member is None:
            raise ValueError("release.json is missing")
        release_file = archive.extractfile(release_member)
        if release_file is None:
            raise ValueError("release.json cannot be read")
        manifest_bytes = release_file.read(MAX_MANIFEST_BYTES + 1)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("release.json is too large")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("invalid release manifest")
        verify_manifest_signature(manifest, public_key_path)

        release_id = manifest.get("release_id")
        if not isinstance(release_id, str) or not RELEASE_ID_PATTERN.fullmatch(release_id):
            raise ValueError("invalid release id")
        file_entries = manifest.get("files")
        if not isinstance(file_entries, list):
            raise ValueError("invalid release file manifest")
        for field in ("schema_version", "min_schema_version", "max_schema_version"):
            if not isinstance(manifest.get(field), int) or int(manifest[field]) < 1:
                raise ValueError("invalid release schema contract")
        if not (
            int(manifest["min_schema_version"])
            <= int(manifest["schema_version"])
            <= int(manifest["max_schema_version"])
        ):
            raise ValueError("invalid release schema contract")

        entries: dict[str, dict[str, object]] = {}
        for raw_entry in file_entries:
            if not isinstance(raw_entry, dict):
                raise ValueError("invalid release file entry")
            name = raw_entry.get("path")
            digest = raw_entry.get("sha256")
            size = raw_entry.get("size")
            if (
                not isinstance(name, str)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(size, int)
                or size < 0
            ):
                raise ValueError("invalid release file entry")
            safe_name = _safe_member_name(name).as_posix()
            if safe_name == "release.json" or safe_name in entries:
                raise ValueError(f"duplicate release file entry: {safe_name}")
            entries[safe_name] = raw_entry

        if set(names) != {*entries, "release.json"}:
            raise ValueError("archive content does not match release manifest")
        if manifest.get("source_sha256") != _source_digest(file_entries):
            raise ValueError("source manifest digest mismatch")

        destination = releases / release_id
        if destination.exists():
            raise FileExistsError(f"release already exists: {release_id}")
        staging = releases / f".{release_id}.installing"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        try:
            for name, entry in entries.items():
                member = names[name]
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"archive member cannot be read: {name}")
                target = staging.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
                if target.stat().st_size != entry["size"]:
                    raise ValueError(f"size mismatch: {name}")
                if _file_digest(target) != entry["sha256"]:
                    raise ValueError(f"digest mismatch: {name}")

            if _file_digest(staging / "uv.lock") != manifest.get("uv_lock_sha256"):
                raise ValueError("uv.lock digest mismatch")
            (staging / "release.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            staging.replace(destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a signed Coderus release archive")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    args = parser.parse_args()
    print(
        install_release_archive(
            args.archive,
            args.root,
            public_key_path=args.public_key,
        )
    )


if __name__ == "__main__":
    main()
