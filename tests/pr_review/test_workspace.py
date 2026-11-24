from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

import coderus.pr_review.workspace as workspace_module
from coderus.pr_review.workspace import PRWorkspace, PRWorkspaceError
from coderus.processes import ProcessResult


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.asyncio
async def test_git_size_check_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces")
    event_loop = asyncio.get_running_loop()
    event_loop_responsive = asyncio.Event()

    async def fake_run_process(*args, **kwargs) -> ProcessResult:
        return ProcessResult(returncode=0, stdout=b"", stderr=b"")

    def slow_size_check(path: Path, limit: int) -> bool:
        event_loop.call_soon_threadsafe(event_loop_responsive.set)
        time.sleep(0.05)
        return False

    monkeypatch.setattr(workspace_module, "run_process", fake_run_process)
    monkeypatch.setattr(workspace_module, "path_size_exceeds", slow_size_check)

    await manager._run("git", "status", cwd=tmp_path)

    assert event_loop_responsive.is_set()


def test_transfer_commands_use_relaxed_timeout(tmp_path: Path) -> None:
    manager = PRWorkspace(
        tmp_path / "workspaces",
        command_timeout_seconds=10,
        transfer_timeout_seconds=600,
    )
    assert manager._timeout_for(("git", "clone", "--", "url", "dst")) == 600
    assert manager._timeout_for(("git", "fetch", "--", "upstream", "main")) == 600
    assert manager._timeout_for(("git", "checkout", "--detach", "a" * 40)) == 600
    assert manager._timeout_for(("git", "rev-parse", "HEAD")) == 10


def create_upstream(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test User")
    git(source, "config", "user.email", "test@example.com")

    source_app = source / "src" / "app.py"
    source_app.parent.mkdir()
    source_app.write_text("".join(f"line {number}\n" for number in range(1, 21)), encoding="utf-8")
    (source / "src" / "deleted.py").write_text("old one\nold two\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    base_sha = git(source, "rev-parse", "HEAD")

    upstream = tmp_path / "upstream.git"
    git(tmp_path, "init", "--bare", str(upstream))
    git(source, "remote", "add", "upstream", upstream.as_uri())
    git(source, "push", "upstream", "main")

    source_app.write_text(
        "".join(f"line {number}\n" for number in range(1, 10))
        + "updated ten\nupdated eleven\nadded twelve\nadded thirteen\n"
        + "".join(f"line {number}\n" for number in range(12, 21)),
        encoding="utf-8",
    )
    (source / "src" / "new.py").write_text("new one\nnew two\n", encoding="utf-8")
    (source / "src" / "deleted.py").unlink()
    git(source, "add", "-A")
    git(source, "commit", "-m", "review changes")
    head_sha = git(source, "rev-parse", "HEAD")
    git(source, "push", "upstream", "HEAD:refs/heads/review-head")
    return upstream, base_sha, head_sha


@pytest.fixture
def allow_local_head_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PRWorkspace, "_validate_head_repository_url", lambda _: None)


@pytest.mark.asyncio
async def test_resolve_remote_head_returns_current_branch_tip(
    tmp_path: Path,
    allow_local_head_repository: None,
) -> None:
    upstream, _, head_sha = create_upstream(tmp_path)
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")

    assert await manager.resolve_remote_head(upstream.as_uri(), "review-head") == head_sha
    assert await manager.resolve_remote_head(upstream.as_uri(), "no-such-branch") is None


@pytest.mark.asyncio
async def test_resolve_remote_head_rejects_unsafe_inputs_before_running_git(
    tmp_path: Path,
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")

    with pytest.raises(ValueError):
        await manager.resolve_remote_head(
            "https://token@github.com/contributor/widgets.git", "main"
        )
    with pytest.raises(ValueError):
        await manager.resolve_remote_head(
            "https://github.com/contributor/widgets.git", "--upload-pack=evil"
        )


@pytest.mark.asyncio
async def test_prepare_replaces_only_expected_directory_with_full_head_checkout(
    tmp_path: Path,
    allow_local_head_repository: None,
) -> None:
    upstream, base_sha, head_sha = create_upstream(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    stale_workspace = workspace_root / "pr-review-3"
    stale_workspace.mkdir()
    (stale_workspace / "stale.txt").write_text("remove me", encoding="utf-8")
    sibling = workspace_root / "pr-review-3-sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep me", encoding="utf-8")

    staging_root = tmp_path / "manager-staging"
    manager = PRWorkspace(workspace_root, staging_root=staging_root)
    workspace = await manager.prepare(
        3, 7, head_sha, "review-head", upstream.as_uri(),
    )

    assert workspace.name == "pr-review-3"
    assert (workspace / ".git").is_dir()
    assert (workspace / "src" / "app.py").is_file()
    assert git(workspace, "rev-parse", "HEAD") == head_sha
    assert git(workspace, "rev-parse", base_sha) == base_sha
    assert not (workspace / "changes.diff").exists()
    assert not (workspace / "metadata.json").exists()
    assert not (workspace / "stale.txt").exists()
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "keep me"
    assert list(staging_root.iterdir()) == []


@pytest.mark.asyncio
async def test_prepare_force_syncs_rewritten_branch_despite_same_named_tag(
    tmp_path: Path, allow_local_head_repository: None
) -> None:
    upstream, base_sha, old_head = create_upstream(tmp_path)
    source = tmp_path / "source"
    git(source, "tag", "review-head", old_head)
    git(source, "push", "upstream", "refs/tags/review-head")
    git(source, "checkout", "--detach", base_sha)
    (source / "fresh.txt").write_text("rewritten branch", encoding="utf-8")
    git(source, "add", "fresh.txt")
    git(source, "commit", "-m", "replacement head")
    new_head = git(source, "rev-parse", "HEAD")
    git(source, "push", "--force", "upstream", "HEAD:refs/heads/review-head")
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "staging")
    workspace = await manager.prepare(
        42, 7, new_head, "review-head", upstream.as_uri(),
    )
    assert git(workspace, "rev-parse", "HEAD") == new_head
    assert (workspace / "fresh.txt").is_file()
    assert not (workspace / "src" / "new.py").exists()


@pytest.mark.asyncio
async def test_prepare_rejects_a_fetched_pr_head_that_does_not_match_requested_sha(
    tmp_path: Path,
    allow_local_head_repository: None,
) -> None:
    upstream, base_sha, _ = create_upstream(tmp_path)
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")

    with pytest.raises(RuntimeError, match="head SHA"):
        await manager.prepare(
            4, 7, base_sha, "review-head", upstream.as_uri(),
        )


@pytest.mark.asyncio
async def test_prepare_fetches_only_the_source_ref_without_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-appear")
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")
    head_sha = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def fake_run(*command: str, cwd: Path) -> str:
        calls.append(command)
        if command[1] == "init":
            Path(command[-1]).mkdir(parents=True)
        if command == ("git", "rev-parse", "FETCH_HEAD^{commit}"):
            return head_sha
        return ""

    monkeypatch.setattr(manager, "_run", fake_run)

    await manager.prepare(
        task_id=8,
        pr_number=17,
        head_sha=head_sha,
        head_ref="feature/review",
        head_repository_url="https://github.com/contributor/widgets.git",
    )
    assert not any(command[1] == "clone" for command in calls)
    assert [command for command in calls if command[1] == "fetch"] == [(
        "git",
        "fetch",
        "--force",
        "--no-tags",
        "--",
        "https://github.com/contributor/widgets.git",
        "refs/heads/feature/review",
    )]
    assert ("git", "checkout", "--detach", head_sha) in calls
    assert not any(command[1:2] == ("diff",) for command in calls)
    assert "must-not-appear" not in repr(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "head_repository_url",
    [
        "https://token@github.com/contributor/widgets.git",
        "https://github.com/../widgets.git",
        "file:///tmp/widgets.git",
    ],
)
async def test_prepare_rejects_credential_bearing_head_repository_url_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head_repository_url: str,
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")
    calls: list[tuple[str, ...]] = []

    async def record_run(*command: str, cwd: Path) -> str:
        calls.append(command)
        return ""

    monkeypatch.setattr(manager, "_run", record_run)

    with pytest.raises(ValueError, match="head_repository_url"):
        await manager.prepare(
            9,
            17,
            "b" * 40,
            "feature/review",
            head_repository_url,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_review_range_contains_only_the_platform_pr_commits(
    tmp_path: Path, allow_local_head_repository: None
) -> None:
    upstream, _, first_change = create_upstream(tmp_path)
    source = tmp_path / "source"
    (source / "follow-up.txt").write_text("Remaining review change", encoding="utf-8")
    git(source, "add", "follow-up.txt")
    git(source, "commit", "-m", "Add another change to the review")
    head_sha = git(source, "rev-parse", "HEAD")
    git(source, "push", "upstream", "HEAD:refs/heads/review-head")
    git(source, "push", "upstream", f"{first_change}:refs/heads/main")
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")
    workspace = await manager.prepare(5, 7, head_sha, "review-head", upstream.as_uri())
    comparison = await manager.comparison_from_commits(workspace, head_sha, frozenset({head_sha}))
    material = await manager.review_input(workspace, comparison, head_sha)
    assert material.ranges.comparison_sha == first_change
    assert "follow-up.txt" in material.unified_diff
    assert "src/app.py" not in material.unified_diff


@pytest.mark.asyncio
async def test_prepare_ignores_an_unrelated_default_branch(
    tmp_path: Path,
    allow_local_head_repository: None,
) -> None:
    upstream, base_sha, head_sha = create_upstream(tmp_path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git(unrelated, "init", "-b", "unrelated")
    git(unrelated, "config", "user.name", "Test User")
    git(unrelated, "config", "user.email", "test@example.com")
    (unrelated / "UNRELATED.md").write_text("unrelated\n", encoding="utf-8")
    git(unrelated, "add", ".")
    git(unrelated, "commit", "-m", "unrelated")
    unrelated_sha = git(unrelated, "rev-parse", "HEAD")
    git(unrelated, "remote", "add", "upstream", upstream.as_uri())
    git(unrelated, "push", "upstream", "unrelated")
    git(upstream, "symbolic-ref", "HEAD", "refs/heads/unrelated")

    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")

    workspace = await manager.prepare(6, 7, head_sha, "review-head", upstream.as_uri())
    with pytest.raises(subprocess.CalledProcessError):
        git(workspace, "cat-file", "-t", unrelated_sha)
    assert await manager.comparison_from_commits(
        workspace, head_sha, frozenset({head_sha}),
    ) == base_sha


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", 0),
        ("task_id", -1),
        ("task_id", True),
        ("pr_number", 0),
        ("pr_number", -1),
        ("pr_number", True),
        ("head_sha", "not-a-sha"),
        ("head_ref", "--upload-pack=evil"),
        ("head_repository_url", "--upload-pack=evil"),
    ],
)
async def test_prepare_rejects_unsafe_inputs_before_running_git(
    tmp_path: Path,
    field: str,
    value: int | str | bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments: dict[str, int | str | bool] = {
        "task_id": 3,
        "pr_number": 7,
        "head_sha": "b" * 40,
        "head_ref": "review-head",
        "head_repository_url": "https://github.com/acme/source.git",
    }
    arguments[field] = value
    manager = PRWorkspace(tmp_path / "workspaces", staging_root=tmp_path / "manager-staging")
    calls: list[tuple[str, ...]] = []

    async def record_run(*command: str, cwd: Path) -> str:
        calls.append(command)
        raise AssertionError("invalid input must not start Git")

    monkeypatch.setattr(manager, "_run", record_run)

    with pytest.raises(ValueError, match=field):
        await manager.prepare(**arguments)  # type: ignore[arg-type]

    assert not manager.workspace_root.exists()
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "head_ref",
    [
        "main:refs/coderus/pr/7",
        "+refs/heads/main:refs/coderus/pr/7",
        "feature/*",
        "feature..main",
        "feature@{upstream}",
        "feature branch",
        "feature\tbranch",
        r"feature\branch",
        "feature.",
        "feature/",
        "feature.lock",
        "feature/branch.lock",
    ],
)
async def test_prepare_rejects_unsafe_source_refs_before_running_git(
    tmp_path: Path,
    head_ref: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces")
    calls: list[tuple[str, ...]] = []

    async def record_run(*command: str, cwd: Path) -> str:
        calls.append(command)
        raise AssertionError("invalid input must not start Git")

    monkeypatch.setattr(manager, "_run", record_run)

    with pytest.raises(ValueError, match="head_ref"):
        await manager.prepare(
            3,
            7,
            "b" * 40,
            head_ref,
            "https://github.com/acme/source.git",
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_sha", "--help"),
        ("base_sha", "a" * 39),
        ("head_sha", "--help"),
        ("head_sha", "b" * 39),
    ],
)
async def test_review_input_rejects_invalid_revisions_before_reading_material(
    tmp_path: Path,
    field: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces")
    arguments = {"base_sha": "a" * 40, "head_sha": "b" * 40}
    arguments[field] = value
    calls: list[tuple[str, ...]] = []

    async def record_run(*command: str, cwd: Path) -> str:
        calls.append(command)
        raise AssertionError("invalid revision must not start Git")

    monkeypatch.setattr(manager, "_run", record_run)

    with pytest.raises(ValueError, match=field):
        await manager.review_input(tmp_path, **arguments)

    assert calls == []


@pytest.mark.asyncio
async def test_review_input_reads_verified_git_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces")
    calls: list[tuple[str, ...]] = []

    async def record_run(*command: str, cwd: Path) -> str:
        calls.append(command)
        if command == ("git", "rev-parse", "HEAD^{commit}"):
            return "b" * 40
        if command == ("git", "merge-base", "a" * 40, "b" * 40):
            return "c" * 40
        if command[:4] == ("git", "diff", "--no-ext-diff", "--no-color"):
            return "\n".join(
                (
                    "diff --git a/src/app.py b/src/app.py",
                    "--- a/src/app.py",
                    "+++ b/src/app.py",
                    "@@ -1 +1 @@",
                    "-before",
                    "+after",
                )
            )
        return ""

    monkeypatch.setattr(manager, "_run", record_run)
    material = await manager.review_input(tmp_path, "a" * 40, "b" * 40)
    ranges = material.ranges

    assert ranges.contains("src/app.py", "LEFT", 1, 1)
    assert ranges.contains("src/app.py", "RIGHT", 1, 1)
    assert ranges.comparison_sha == "c" * 40
    assert ranges.changed_file_count == 1
    assert ranges.additions == 1
    assert ranges.deletions == 1
    assert material.review_base == "coderus-review-base"
    assert "@@ -1 +1 @@" in material.unified_diff
    assert ("git", "cat-file", "-e", f"{'a' * 40}^{{commit}}") in calls
    assert ("git", "merge-base", "a" * 40, "b" * 40) in calls
    assert (
        "git",
        "update-ref",
        "refs/heads/coderus-review-base",
        "c" * 40,
    ) in calls
    diff_call = next(call for call in calls if "--unified=5" in call)
    assert diff_call[-3:-1] == ("c" * 40, "b" * 40)
    assert "--unified=5" in diff_call
    assert not any("--name-status" in call or "--stat=200" in call for call in calls)


@pytest.mark.asyncio
async def test_review_input_rejects_checkout_at_a_different_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces")

    async def wrong_head(*command: str, cwd: Path) -> str:
        return "c" * 40

    monkeypatch.setattr(manager, "_run", wrong_head)

    with pytest.raises(RuntimeError, match="revision"):
        await manager.review_input(tmp_path, "a" * 40, "b" * 40)


@pytest.mark.asyncio
async def test_assert_pristine_rejects_workspace_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PRWorkspace(tmp_path / "workspaces")

    async def record_run(*command: str, cwd: Path) -> str:
        if command == ("git", "rev-parse", "HEAD^{commit}"):
            return "b" * 40
        if command == (
            "git",
            "rev-parse",
            "refs/heads/coderus-review-base^{commit}",
        ):
            return "c" * 40
        if command == ("git", "status", "--porcelain=v1", "--untracked-files=all"):
            return " M src/app.py\n"
        raise AssertionError(command)

    monkeypatch.setattr(manager, "_run", record_run)

    with pytest.raises(RuntimeError, match="workspace was modified"):
        await manager.assert_pristine(tmp_path, "b" * 40, "c" * 40)


def test_git_environment_excludes_github_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("PATH", os.environ["PATH"])

    isolated_home = tmp_path / "manager-home"
    isolated_home.mkdir()
    environment = PRWorkspace._git_environment(isolated_home)

    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["HOME"] == str(isolated_home)
    assert environment["USERPROFILE"] == str(isolated_home)
    assert "GITHUB_TOKEN" not in environment
    assert "GH_TOKEN" not in environment
    assert "github-secret" not in environment.values()
    assert "gh-secret" not in environment.values()


@pytest.mark.asyncio
async def test_prepare_ignores_hostile_global_url_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_local_head_repository: None,
) -> None:
    upstream, _, head_sha = create_upstream(tmp_path)
    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    (hostile_home / ".gitconfig").write_text(
        f'[url "file:///definitely/missing"]\n\tinsteadOf = {upstream.as_uri()}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(hostile_home))
    monkeypatch.setenv("USERPROFILE", str(hostile_home))

    workspace = await PRWorkspace(
        tmp_path / "workspaces", staging_root=tmp_path / "staging"
    ).prepare(
        8,
        7,
        head_sha,
        "review-head",
        upstream.as_uri(),
    )

    assert (workspace / ".git").is_dir()
    assert git(workspace, "rev-parse", "HEAD") == head_sha


@pytest.mark.asyncio
async def test_review_input_tracks_modified_added_and_deleted_lines(
    tmp_path: Path, allow_local_head_repository: None
) -> None:
    upstream, base_sha, head_sha = create_upstream(tmp_path)
    manager = PRWorkspace(tmp_path / "workspaces")
    workspace = await manager.prepare(3, 7, head_sha, "review-head", upstream.as_uri())

    material = await manager.review_input(workspace, base_sha, head_sha)
    ranges = material.ranges

    assert material.review_base == "coderus-review-base"
    assert git(workspace, "rev-parse", "refs/heads/coderus-review-base") == base_sha
    assert ranges.ranges == {
        ("src/app.py", "LEFT"): ((10, 11),),
        ("src/app.py", "RIGHT"): ((10, 13),),
        ("src/new.py", "RIGHT"): ((1, 2),),
        ("src/deleted.py", "LEFT"): ((1, 2),),
    }
    assert ranges.changed_file_count == 3
    assert ranges.additions == 6
    assert ranges.deletions == 4


@pytest.mark.asyncio
async def test_comparison_from_commits_handles_merge_history_and_rejects_incomplete_ranges(
    tmp_path: Path, allow_local_head_repository: None
) -> None:
    upstream, base_sha, first = create_upstream(tmp_path)
    source = tmp_path / "source"
    git(source, "checkout", "--detach", base_sha)
    git(source, "commit", "--allow-empty", "-m", "Unrelated target change")
    target = git(source, "rev-parse", "HEAD")
    git(source, "checkout", "--detach", first)
    git(source, "merge", "--no-ff", target, "-m", "Merge target into source")
    merged = git(source, "rev-parse", "HEAD")
    git(source, "commit", "--allow-empty", "-m", "Finish source work")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "upstream", "HEAD:refs/heads/review-head")

    manager = PRWorkspace(tmp_path / "workspaces")
    workspace = await manager.prepare(3, 7, head, "review-head", upstream.as_uri())
    comparison = await manager.comparison_from_commits(
        workspace, head, frozenset({head, first, merged}),
    )
    assert comparison == target
    with pytest.raises(PRWorkspaceError, match="源分支历史不一致"):
        await manager.comparison_from_commits(workspace, head, frozenset({head, first}))


@pytest.mark.asyncio
async def test_can_follow_up_requires_ancestor_and_unchanged_standards(tmp_path: Path) -> None:
    _, base_sha, head_sha = create_upstream(tmp_path)
    source = tmp_path / "source"
    manager = PRWorkspace(tmp_path / "workspaces")
    assert await manager.can_follow_up(source, base_sha, head_sha)
    assert not await manager.can_follow_up(source, head_sha, base_sha)

    (source / "CONTRIBUTING.md").write_text("New coding standards", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "Update review rules")
    assert not await manager.can_follow_up(source, head_sha, git(source, "rev-parse", "HEAD"))


def test_changed_ranges_ignores_header_like_hunk_content() -> None:
    ranges = PRWorkspace._parse_changed_ranges(
        "\n".join(
            [
                "diff --git a/src/app.py b/src/app.py",
                "--- a/src/app.py",
                "+++ b/src/app.py",
                "@@ -1 +1 @@",
                "--- a/src/ghost.py",
                "+++ b/src/ghost.py",
                "@@ -1 +1 @@",
            ]
        )
    )

    assert ranges.contains("src/app.py", "LEFT", 1, 1)
    assert ranges.contains("src/app.py", "RIGHT", 1, 1)
    assert not ranges.contains("src/ghost.py", "LEFT", 1, 1)


def test_changed_ranges_decodes_c_style_quoted_paths() -> None:
    ranges = PRWorkspace._parse_changed_ranges(
        "\n".join(
            [
                'diff --git "a/src/quoted" "b/src/quoted"',
                r'--- "a/src/\346\265\213\350\257\225 space\t\"name.py"',
                r'+++ "b/src/\346\265\213\350\257\225 space\t\"name.py"',
                "@@ -1 +1 @@",
                "-before",
                "+after",
            ]
        )
    )
    path = 'src/\u6d4b\u8bd5 space\t"name.py'

    assert ranges.contains(path, "LEFT", 1, 1)
    assert ranges.contains(path, "RIGHT", 1, 1)
