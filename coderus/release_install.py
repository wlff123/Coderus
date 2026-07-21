from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from coderus.release_manifest import build_source_manifest

RELEASE_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


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


def install_release_archive(archive_path: Path, root: Path) -> Path:
    root = root.resolve()
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        names: dict[str, tarfile.TarInfo] = {}
        for member in members:
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
        manifest = json.loads(release_file.read().decode("utf-8"))
        release_id = manifest.get("release_id")
        if not isinstance(release_id, str) or not RELEASE_ID_PATTERN.fullmatch(release_id):
            raise ValueError("invalid release id")
        file_entries = manifest.get("files")
        if not isinstance(file_entries, list):
            raise ValueError("invalid release file manifest")

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


def _environment_digest(environment: Path) -> str:
    if not environment.is_dir():
        raise ValueError("release environment is missing")
    digest = hashlib.sha256()
    for path in sorted(environment.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() and not path.is_symlink():
            continue
        relative = path.relative_to(environment).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link:")
            digest.update(path.readlink().as_posix().encode("utf-8"))
        else:
            digest.update(_file_digest(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _release_manifest(release: Path) -> dict[str, object]:
    try:
        manifest = json.loads((release / "release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("invalid release.json") from exc
    if not isinstance(manifest, dict):
        raise ValueError("invalid release.json")
    return manifest


def write_verification(
    release: Path,
    *,
    verified_at: str | None = None,
) -> Path:
    release = release.expanduser().resolve()
    manifest = _release_manifest(release)
    source = build_source_manifest(release)
    if source["source_sha256"] != manifest.get("source_sha256"):
        raise ValueError("source changed before verification")
    if _file_digest(release / "uv.lock") != manifest.get("uv_lock_sha256"):
        raise ValueError("uv.lock changed before verification")
    payload = {
        "release_id": manifest.get("release_id"),
        "source_sha256": source["source_sha256"],
        "uv_lock_sha256": manifest.get("uv_lock_sha256"),
        "environment_sha256": _environment_digest(release / ".venv"),
        "verified_at": verified_at
        or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    destination = release / "VERIFIED"
    temporary = release / ".VERIFIED.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def validate_verification(release: Path) -> None:
    if release.is_symlink():
        raise ValueError("release directory must not be a symlink")
    release = release.expanduser().resolve()
    manifest = _release_manifest(release)
    try:
        verification = json.loads((release / "VERIFIED").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("release is not verified") from exc
    source = build_source_manifest(release)
    if (
        source["source_sha256"] != manifest.get("source_sha256")
        or verification.get("source_sha256") != source["source_sha256"]
    ):
        raise ValueError("source changed after verification")
    uv_lock_sha256 = _file_digest(release / "uv.lock")
    if (
        uv_lock_sha256 != manifest.get("uv_lock_sha256")
        or verification.get("uv_lock_sha256") != uv_lock_sha256
    ):
        raise ValueError("uv.lock changed after verification")
    if verification.get("environment_sha256") != _environment_digest(release / ".venv"):
        raise ValueError("environment changed after verification")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Install a Coderus release archive")
    parser.add_argument("archive", type=Path, nargs="?")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--write-verification", type=Path)
    parser.add_argument("--verify-release", type=Path)
    args = parser.parse_args()
    if args.write_verification is not None:
        print(write_verification(args.write_verification))
        return
    if args.verify_release is not None:
        validate_verification(args.verify_release)
        print("verified")
        return
    if args.archive is None or args.root is None:
        parser.error("archive and --root are required for installation")
    print(install_release_archive(args.archive, args.root))


if __name__ == "__main__":
    main()
