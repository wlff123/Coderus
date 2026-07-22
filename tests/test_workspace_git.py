import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from coderus.workflow.workspace_git import WorkspaceGit


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_workspace_git_prepares_seals_and_commits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("before\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init")

    workspace_git = WorkspaceGit(tmp_path / "workspaces")
    prepared = await workspace_git.prepare(
        1,
        str(source),
        "main",
        "coderus/issue-1-1",
    )
    with pytest.raises(ValueError, match="did not produce any code changes"):
        await workspace_git.assert_has_changes(prepared.workspace)
    (prepared.workspace / "README.md").write_text("after\n", encoding="utf-8")
    await workspace_git.assert_has_changes(prepared.workspace)
    sealed = await workspace_git.seal(prepared.workspace, tmp_path / "artifacts" / "fixed.patch")
    await workspace_git.assert_no_secrets(prepared.workspace)
    commit_sha = await workspace_git.commit(
        prepared.workspace,
        "Fix issue",
        "Coderus Bot",
        "bot@example.com",
    )

    assert "before" in sealed.patch_path.read_text(encoding="utf-8")
    assert len(sealed.tree_sha) == 40
    assert len(commit_sha) == 40
    assert git(prepared.workspace, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_workspace_git_blocks_high_confidence_secret_in_staged_patch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("before\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init")
    workspace_git = WorkspaceGit(tmp_path / "workspaces")
    prepared = await workspace_git.prepare(1, str(source), "main", "coderus/issue-1-1")
    (prepared.workspace / "secret.txt").write_text(
        "token=" + "ghp_" + "123456789012345678901234567890123456\n",
        encoding="utf-8",
    )
    await workspace_git.seal(prepared.workspace, tmp_path / "fixed.patch")

    with pytest.raises(ValueError, match="credential"):
        await workspace_git.assert_no_secrets(prepared.workspace)


@pytest.mark.asyncio
async def test_workspace_git_ignores_codex_runtime_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("before\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "init",
    )
    workspace_git = WorkspaceGit(tmp_path / "workspaces")
    prepared = await workspace_git.prepare(1, str(source), "main", "coderus/issue-1-1")
    runtime_dir = prepared.workspace / ".coderus" / "tmp"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "policy-test.ps1").write_text("temporary", encoding="utf-8")
    (prepared.workspace / "a1b2c3d4").write_text("blat", encoding="utf-8")

    with pytest.raises(ValueError, match="did not produce any code changes"):
        await workspace_git.assert_has_changes(prepared.workspace)

    (prepared.workspace / "README.md").write_text("after\n", encoding="utf-8")
    sealed = await workspace_git.seal(prepared.workspace, tmp_path / "fixed.patch")
    commit_sha = await workspace_git.commit(
        prepared.workspace,
        "Fix issue",
        "Coderus Bot",
        "bot@example.com",
    )

    assert "a1b2c3d4" not in sealed.patch_path.read_text(encoding="utf-8")
    assert git(prepared.workspace, "ls-tree", "-r", "--name-only", commit_sha) == "README.md"
    assert git(prepared.workspace, "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.asyncio
async def test_workspace_git_ignores_global_hooks_and_removes_local_executable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README.md").write_text("before\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "init",
    )

    hostile_home = tmp_path / "hostile-home"
    hostile_hooks = tmp_path / "hostile-hooks"
    hostile_home.mkdir()
    hostile_hooks.mkdir()
    global_marker = tmp_path / "global-hook-ran.txt"
    post_checkout = hostile_hooks / "post-checkout"
    post_checkout.write_text(
        f"#!/bin/sh\necho hostile > {shlex.quote(global_marker.as_posix())}\n",
        encoding="utf-8",
    )
    post_checkout.chmod(0o700)
    (hostile_home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {hostile_hooks.as_posix()}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(hostile_home))
    monkeypatch.setenv("USERPROFILE", str(hostile_home))

    manager = WorkspaceGit(tmp_path / "workspaces")
    prepared = await manager.prepare(1, str(source), "main", "coderus/issue-1-1")
    assert not global_marker.exists()

    executable_marker = tmp_path / "local-config-ran.txt"
    filter_script = tmp_path / "filter.py"
    filter_script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    command = shlex.join(
        [
            Path(sys.executable).as_posix(),
            filter_script.as_posix(),
            executable_marker.as_posix(),
        ]
    )
    (prepared.workspace / ".gitattributes").write_text(
        "filtered.txt filter=hostile\n", encoding="utf-8"
    )
    (prepared.workspace / "filtered.txt").write_text("content\n", encoding="utf-8")
    git(prepared.workspace, "config", "filter.hostile.clean", command)
    git(prepared.workspace, "config", "filter.hostile.required", "true")
    git(prepared.workspace, "config", "core.fsmonitor", command)

    sealed = await manager.seal(
        prepared.workspace, tmp_path / "artifacts" / "hostile.patch"
    )
    await manager.commit(
        prepared.workspace,
        "Safe commit",
        "Coderus Bot",
        "bot@example.com",
    )

    assert sealed.patch_path.exists()
    assert not executable_marker.exists()


def initialize_source(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "README.md").write_text("before\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "init",
    )


@pytest.mark.asyncio
async def test_seal_rejects_workspace_over_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_source(source)
    workspace_root = tmp_path / "workspaces"
    prepared = await WorkspaceGit(workspace_root).prepare(
        1, str(source), "main", "coderus/issue-1-1"
    )
    manager = WorkspaceGit(workspace_root, max_workspace_bytes=64)
    (prepared.workspace / "large.bin").write_bytes(b"x" * 65)

    with pytest.raises(ValueError, match="workspace size limit"):
        await manager.seal(prepared.workspace, tmp_path / "fixed.patch")


@pytest.mark.asyncio
async def test_prepare_removes_partial_clone_when_workspace_limit_is_exceeded(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    initialize_source(source)
    workspace_root = tmp_path / "workspaces"
    manager = WorkspaceGit(workspace_root, max_workspace_bytes=64)

    with pytest.raises(RuntimeError, match="workspace size limit"):
        await manager.prepare(1, str(source), "main", "coderus/issue-1-1")

    assert not (workspace_root / "task-1").exists()


@pytest.mark.asyncio
async def test_seal_rejects_too_many_changed_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_source(source)
    manager = WorkspaceGit(tmp_path / "workspaces", max_changed_files=1)
    prepared = await manager.prepare(1, str(source), "main", "coderus/issue-1-1")
    (prepared.workspace / "one.txt").write_text("one", encoding="utf-8")
    (prepared.workspace / "two.txt").write_text("two", encoding="utf-8")

    with pytest.raises(ValueError, match="changed file limit"):
        await manager.seal(prepared.workspace, tmp_path / "fixed.patch")


@pytest.mark.asyncio
async def test_seal_rejects_patch_over_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    initialize_source(source)
    manager = WorkspaceGit(tmp_path / "workspaces", max_patch_bytes=32)
    prepared = await manager.prepare(1, str(source), "main", "coderus/issue-1-1")
    (prepared.workspace / "README.md").write_text("after " + "x" * 100, encoding="utf-8")

    with pytest.raises(ValueError, match="patch size limit"):
        await manager.seal(prepared.workspace, tmp_path / "fixed.patch")
