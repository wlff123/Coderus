from __future__ import annotations

from os import PathLike
from pathlib import Path


class WorkspaceError(ValueError):
    pass


def validate_workspace(workspace_root: str | PathLike[str], requested: str | PathLike[str]) -> Path:
    try:
        root = Path(workspace_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError("workspace_root must be an existing directory") from exc
    if not root.is_dir():
        raise WorkspaceError("workspace_root must be an existing directory")
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise WorkspaceError("workspace must be below workspace_root") from exc

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError("workspace must be an existing directory") from exc

    if not resolved.is_dir():
        raise WorkspaceError("workspace must be an existing directory")
    return resolved
