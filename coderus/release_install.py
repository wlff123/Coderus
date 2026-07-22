from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from coderus.release_bootstrap import (
    _file_digest,
    install_release_archive,
    verify_manifest_signature,
)
from coderus.release_manifest import build_source_manifest


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
    public_key_path: Path | None = None,
) -> Path:
    release = release.expanduser().resolve()
    manifest = _release_manifest(release)
    verify_manifest_signature(manifest, public_key_path)
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


def validate_verification(
    release: Path,
    *,
    public_key_path: Path | None = None,
) -> None:
    if release.is_symlink():
        raise ValueError("release directory must not be a symlink")
    release = release.expanduser().resolve()
    manifest = _release_manifest(release)
    verify_manifest_signature(manifest, public_key_path)
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
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--write-verification", type=Path)
    parser.add_argument("--verify-release", type=Path)
    args = parser.parse_args()
    if args.write_verification is not None:
        if args.public_key is None:
            parser.error("--public-key is required for verification")
        print(
            write_verification(
                args.write_verification,
                public_key_path=args.public_key,
            )
        )
        return
    if args.verify_release is not None:
        if args.public_key is None:
            parser.error("--public-key is required for verification")
        validate_verification(args.verify_release, public_key_path=args.public_key)
        print("verified")
        return
    if args.archive is None or args.root is None or args.public_key is None:
        parser.error("archive, --root and --public-key are required for installation")
    print(
        install_release_archive(
            args.archive,
            args.root,
            public_key_path=args.public_key,
        )
    )


if __name__ == "__main__":
    main()
