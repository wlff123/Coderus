from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from coderus.processes import (
    CommandOutputLimitExceeded,
    CommandResourceLimitExceeded,
    CommandTimedOut,
    path_size_exceeds,
    run_process,
)

_CODEX_PROBE_NAME = re.compile(r"^[a-z0-9_]{8}$")
_SYSTEM_ENVIRONMENT_KEYS = {
    "COMSPEC",
    "LANG",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}
_LOCAL_EXECUTABLE_CONFIG = {
    "core.askpass",
    "core.attributesfile",
    "core.editor",
    "core.excludesfile",
    "core.fsmonitor",
    "core.hookspath",
    "core.pager",
    "core.sshcommand",
    "core.worktree",
    "credential.helper",
    "diff.external",
    "extensions.worktreeconfig",
    "gpg.program",
    "gpg.ssh.defaultkeycommand",
    "sequence.editor",
}


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    workspace: Path
    base_commit_sha: str
    branch: str


@dataclass(frozen=True, slots=True)
class SealedPatch:
    patch_path: Path
    tree_sha: str


class WorkspaceGit:
    def __init__(
        self,
        workspace_root: Path,
        *,
        command_timeout_seconds: float = 120.0,
        max_workspace_bytes: int = 2 * 1024 * 1024 * 1024,
        max_changed_files: int = 500,
        max_patch_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        for name, value in (
            ("max_workspace_bytes", max_workspace_bytes),
            ("max_changed_files", max_changed_files),
            ("max_patch_bytes", max_patch_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.workspace_root = workspace_root.expanduser().resolve()
        self.command_timeout_seconds = command_timeout_seconds
        self.max_workspace_bytes = max_workspace_bytes
        self.max_changed_files = max_changed_files
        self.max_patch_bytes = max_patch_bytes

    async def prepare(
        self,
        task_id: int,
        repository_url: str,
        default_branch: str,
        branch: str,
    ) -> PreparedWorkspace:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = (self.workspace_root / f"task-{task_id}").resolve()
        if workspace.parent != self.workspace_root:
            raise ValueError("task workspace escapes workspace root")
        if workspace.exists():
            raise FileExistsError(f"task workspace already exists: {workspace}")
        try:
            await self._run(
                "git",
                "clone",
                "--no-tags",
                "--origin",
                "upstream",
                "--branch",
                default_branch,
                repository_url,
                str(workspace),
                cwd=self.workspace_root,
            )
        except BaseException:
            if workspace.is_symlink():
                workspace.unlink(missing_ok=True)
            elif workspace.exists() and workspace.parent == self.workspace_root:
                shutil.rmtree(workspace, onexc=self._remove_readonly)
            raise
        await self._run("git", "checkout", "-b", branch, cwd=workspace)
        self._ignore_runtime_artifacts(workspace)
        base_commit_sha = (await self._run("git", "rev-parse", "HEAD", cwd=workspace)).strip()
        return PreparedWorkspace(workspace, base_commit_sha, branch)

    async def seal(self, workspace: Path, patch_path: Path) -> SealedPatch:
        self._ignore_runtime_artifacts(workspace)
        if self._workspace_size(workspace) > self.max_workspace_bytes:
            raise ValueError("workspace size limit exceeded")
        await self._run("git", "add", "-A", cwd=workspace)
        changed = await self._run(
            "git", "diff", "--cached", "--name-only", "-z", "HEAD", cwd=workspace
        )
        if len([name for name in changed.split("\0") if name]) > self.max_changed_files:
            raise ValueError("changed file limit exceeded")
        patch = await self._run(
            "git", "diff", "--no-ext-diff", "--cached", "--binary", "HEAD", cwd=workspace
        )
        if not patch.strip():
            raise ValueError("Codex did not produce any code changes")
        if len(patch.encode("utf-8")) > self.max_patch_bytes:
            raise ValueError("patch size limit exceeded")
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(patch, encoding="utf-8")
        tree_sha = (await self._run("git", "write-tree", cwd=workspace)).strip()
        return SealedPatch(patch_path, tree_sha)

    async def assert_has_changes(self, workspace: Path) -> None:
        self._ignore_runtime_artifacts(workspace)
        status = await self._run(
            "git", "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if not status.strip():
            raise ValueError("Codex did not produce any code changes")

    async def assert_no_secrets(self, workspace: Path) -> None:
        patch = await self._run(
            "git", "diff", "--cached", "--unified=0", "--no-ext-diff", cwd=workspace
        )
        added = "\n".join(
            line[1:]
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        patterns = (
            r"ghp_[A-Za-z0-9]{36,}",
            r"github_pat_[A-Za-z0-9_]{40,}",
            r"sk-[A-Za-z0-9_-]{20,}",
            r"AKIA[0-9A-Z]{16}",
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        )
        if any(re.search(pattern, added) for pattern in patterns):
            raise ValueError("staged changes contain a high-confidence credential")

    async def assert_branch(self, workspace: Path, expected_branch: str) -> None:
        branch = (await self._run("git", "branch", "--show-current", cwd=workspace)).strip()
        if branch != expected_branch:
            raise ValueError("task workspace is on an unexpected branch")

    async def assert_clean_commit(self, workspace: Path, expected_commit: str) -> None:
        self._ignore_runtime_artifacts(workspace)
        commit = (await self._run("git", "rev-parse", "HEAD", cwd=workspace)).strip()
        status = await self._run(
            "git", "status", "--porcelain", "--untracked-files=all", cwd=workspace
        )
        if commit != expected_commit or status.strip():
            raise ValueError("task workspace no longer matches its committed result")

    def _ignore_runtime_artifacts(self, workspace: Path) -> None:
        workspace, git_dir = self._validated_repository_paths(workspace)
        patterns = {"/.coderus/"}
        for path in workspace.iterdir():
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if (
                stat.S_ISREG(mode)
                and _CODEX_PROBE_NAME.fullmatch(path.name)
                and path.stat().st_size == 4
                and path.read_bytes() == b"blat"
            ):
                patterns.add(f"/{path.name}")

        info_path = git_dir / "info"
        self._require_real_directory(info_path, "Git info directory")
        exclude_path = info_path / "exclude"
        self._require_real_file(exclude_path, "Git exclude file")
        existing = set(exclude_path.read_text(encoding="utf-8").splitlines())
        additions = sorted(patterns - existing)
        if additions:
            with exclude_path.open("a", encoding="utf-8") as exclude_file:
                exclude_file.write("".join(f"{pattern}\n" for pattern in additions))

    async def commit(
        self,
        workspace: Path,
        title: str,
        user_name: str,
        user_email: str,
    ) -> str:
        null_hooks = "NUL" if os.name == "nt" else "/dev/null"
        await self._run(
            "git",
            "-c",
            f"user.name={user_name}",
            "-c",
            f"user.email={user_email}",
            "-c",
            f"core.hooksPath={null_hooks}",
            "commit",
            "-m",
            title,
            cwd=workspace,
        )
        return (await self._run("git", "rev-parse", "HEAD", cwd=workspace)).strip()

    async def assert_tree(self, workspace: Path, expected_tree_sha: str) -> None:
        actual = (await self._run("git", "write-tree", cwd=workspace)).strip()
        if actual != expected_tree_sha:
            raise ValueError("working tree changed after review confirmation")

    async def assert_committed_tree(
        self, workspace: Path, commit_sha: str, expected_tree_sha: str
    ) -> None:
        actual = (
            await self._run("git", "rev-parse", f"{commit_sha}^{{tree}}", cwd=workspace)
        ).strip()
        if actual != expected_tree_sha:
            raise ValueError("committed tree differs from the reviewed tree")

    async def _run(self, *command: str, cwd: Path) -> str:
        if not command or command[0] != "git":
            raise ValueError("WorkspaceGit only permits Git commands")
        cwd = cwd.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="coderus-manager-git-") as isolated:
            isolated_home = Path(isolated)
            hooks_path = isolated_home / "hooks"
            hooks_path.mkdir(mode=0o700)
            environment = self._git_environment(isolated_home)
            if (cwd / ".git").exists():
                self._validated_repository_paths(cwd)
                await self._sanitize_local_config(cwd, environment, hooks_path)
            result = await self._execute_git(command, cwd, environment, hooks_path)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"command failed (git): {message[-1000:]}")
        return result.stdout.decode("utf-8", errors="replace")

    async def _sanitize_local_config(
        self, cwd: Path, environment: dict[str, str], hooks_path: Path
    ) -> None:
        listed = await self._execute_git(
            ("git", "config", "--local", "--no-includes", "--name-only", "--list"),
            cwd,
            environment,
            hooks_path,
        )
        if listed.returncode != 0:
            raise RuntimeError("unable to inspect local Git configuration")
        keys = {
            key.strip()
            for key in listed.stdout.decode("utf-8", errors="replace").splitlines()
            if self._is_executable_config_key(key.strip())
        }
        for key in sorted(keys):
            removed = await self._execute_git(
                ("git", "config", "--local", "--no-includes", "--unset-all", key),
                cwd,
                environment,
                hooks_path,
            )
            if removed.returncode not in {0, 5}:
                raise RuntimeError("unable to remove unsafe local Git configuration")

    async def _execute_git(
        self,
        command: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
        hooks_path: Path,
    ):
        hardened = (
            "git",
            "-c",
            f"core.hooksPath={hooks_path}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "credential.helper=",
            *command[1:],
        )
        try:
            watch_path = (
                Path(command[-1]).resolve(strict=False)
                if len(command) > 2 and command[1] == "clone"
                else cwd
            )
            result = await run_process(
                hardened,
                cwd=cwd,
                env=environment,
                timeout_seconds=self.command_timeout_seconds,
                watch_path=watch_path,
                max_path_bytes=self.max_workspace_bytes,
            )
            if path_size_exceeds(watch_path, self.max_workspace_bytes):
                raise CommandResourceLimitExceeded("command path size limit exceeded")
            return result
        except CommandTimedOut:
            raise RuntimeError("Git command timed out") from None
        except CommandOutputLimitExceeded:
            raise RuntimeError("Git command output limit exceeded") from None
        except CommandResourceLimitExceeded:
            raise RuntimeError("workspace size limit exceeded") from None

    def _workspace_size(self, workspace: Path) -> int:
        total = 0
        pending = [workspace]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError as exc:
                        raise RuntimeError("unable to measure task workspace") from exc
                    if total > self.max_workspace_bytes:
                        return total
        return total

    @staticmethod
    def _is_executable_config_key(key: str) -> bool:
        lowered = key.lower()
        if lowered in _LOCAL_EXECUTABLE_CONFIG:
            return True
        if lowered == "include.path" or (
            lowered.startswith("includeif.") and lowered.endswith(".path")
        ):
            return True
        if lowered.startswith("filter.") and lowered.endswith(
            (".clean", ".smudge", ".process", ".required")
        ):
            return True
        if lowered.startswith("diff.") and lowered.endswith(
            (".command", ".textconv", ".cachetextconv")
        ):
            return True
        if lowered.startswith("gpg.") and lowered.endswith(".program"):
            return True
        if lowered.startswith("pager."):
            return True
        if lowered.startswith("url.") and lowered.endswith(
            (".insteadof", ".pushinsteadof")
        ):
            return True
        if lowered.startswith("remote.") and lowered.endswith(
            (".uploadpack", ".receivepack")
        ):
            return True
        return lowered.startswith("submodule.") and lowered.endswith(".update")

    def _validated_repository_paths(self, workspace: Path) -> tuple[Path, Path]:
        try:
            resolved = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("unable to resolve task workspace") from exc
        if resolved.parent != self.workspace_root or not resolved.is_dir():
            raise RuntimeError("task workspace escapes workspace root")
        git_dir = resolved / ".git"
        self._require_real_directory(git_dir, "Git metadata directory")
        self._require_real_file(git_dir / "config", "Git config file")
        return resolved, git_dir

    @staticmethod
    def _remove_readonly(function, path: str, _error: BaseException) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    @staticmethod
    def _require_real_directory(path: Path, label: str) -> None:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"{label} is unavailable") from exc
        if not stat.S_ISDIR(mode) or path.resolve(strict=True) != path:
            raise RuntimeError(f"{label} must be a real directory")

    @staticmethod
    def _require_real_file(path: Path, label: str) -> None:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(f"{label} is unavailable") from exc
        if not stat.S_ISREG(mode) or path.resolve(strict=True) != path:
            raise RuntimeError(f"{label} must be a real file")

    @staticmethod
    def _git_environment(isolated_home: Path) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in _SYSTEM_ENVIRONMENT_KEYS
        }
        environment["HOME"] = str(isolated_home)
        environment["USERPROFILE"] = str(isolated_home)
        environment["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_PAGER"] = "cat"
        environment["GCM_INTERACTIVE"] = "Never"
        return environment
