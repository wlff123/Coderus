from __future__ import annotations

import json
import os
from pathlib import Path

from coderus.config import Settings


def release_root(settings: Settings) -> Path:
    configured = os.environ.get("CODERUS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    database_parent = settings.database.path.expanduser().resolve().parent
    return database_parent.parent if database_parent.name == "data" else database_parent


def _read_pointer(root: Path, name: str) -> dict[str, str] | None:
    pointer = root / name
    if not pointer.exists():
        return None
    target = pointer.resolve()
    manifest_path = target / "release.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        manifest = {}
    return {
        "release_id": str(manifest.get("release_id") or target.name),
        "created_at": str(manifest.get("created_at") or ""),
    }


def load_release_status(settings: Settings) -> dict[str, dict[str, str] | None]:
    root = release_root(settings)
    return {
        "current": _read_pointer(root, "current"),
        "previous": _read_pointer(root, "previous"),
    }
