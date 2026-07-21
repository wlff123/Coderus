from pathlib import Path

import pytest

from coderus.runner import WorkspaceError, validate_workspace


def test_validate_workspace_accepts_a_directory_below_root(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    workspace = root / "task-1"
    workspace.mkdir(parents=True)

    assert validate_workspace(root, workspace) == workspace.resolve()


@pytest.mark.parametrize("requested", ["../outside", "task-1/../../outside"])
def test_validate_workspace_rejects_relative_escape(tmp_path: Path, requested: str) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()

    with pytest.raises(WorkspaceError, match="workspace_root"):
        validate_workspace(root, requested)


def test_validate_workspace_rejects_absolute_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(WorkspaceError, match="workspace_root"):
        validate_workspace(root, outside)


def test_validate_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(WorkspaceError, match="workspace_root"):
        validate_workspace(root, link)


def test_validate_workspace_requires_an_existing_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()

    with pytest.raises(WorkspaceError, match="existing directory"):
        validate_workspace(root, "missing")


def test_validate_workspace_requires_an_existing_root(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="workspace_root"):
        validate_workspace(tmp_path / "missing-root", "task-1")
