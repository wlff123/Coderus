from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from coderus.forge.errors import InvalidProviderUrl
from coderus.forge.urls import parse_repository_url
from coderus.processes import (
    CommandResourceLimitExceeded,
    CommandTimedOut,
    path_size_exceeds,
    run_process,
)

from .models import ChangedRanges, ReviewInput, normalize_repository_path

_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SHA = re.compile(r"[0-9A-Fa-f]{40}\Z")
REVIEW_BASE = "coderus-review-base"
_INVALID_REF_CHARACTER = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SYSTEM_ENVIRONMENT_KEYS = {
    "COMSPEC",
    "HOME",
    "LANG",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


class PRWorkspace:
    def __init__(
        self,
        workspace_root: Path,
        *,
        staging_root: Path | None = None,
        command_timeout_seconds: float = 120.0,
        transfer_timeout_seconds: float = 1800.0,
        max_workspace_bytes: int = 8 * 1024 * 1024 * 1024,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if transfer_timeout_seconds <= 0:
            raise ValueError("transfer_timeout_seconds must be positive")
        if max_workspace_bytes <= 0:
            raise ValueError("max_workspace_bytes must be positive")
        self.workspace_root = workspace_root.expanduser().resolve()
        suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
        self.staging_root = (
            Path(staging_root)
            if staging_root is not None
            else Path(tempfile.gettempdir()) / f"coderus-pr-review-staging{suffix}"
        )
        self.command_timeout_seconds = command_timeout_seconds
        self.transfer_timeout_seconds = transfer_timeout_seconds
        self.max_workspace_bytes = max_workspace_bytes

    async def prepare(
        self,
        task_id: int,
        repository_url: str,
        pr_number: int,
        base_ref: str,
        base_sha: str,
        head_sha: str,
        head_ref: str,
        head_repository_url: str,
    ) -> Path:
        self._validate_prepare_inputs(
            task_id,
            repository_url,
            pr_number,
            base_ref,
            base_sha,
            head_sha,
            head_ref,
            head_repository_url,
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace_name = f"pr-review-{task_id}"
        workspace = (self.workspace_root / workspace_name).resolve(strict=False)
        if workspace.parent != self.workspace_root or workspace.name != workspace_name:
            raise RuntimeError("PR workspace escapes workspace root")
        if workspace.exists():
            # 陈旧工作区可能有数 GB，删除必须离开事件循环执行。
            await asyncio.to_thread(self._remove_stale_workspace, workspace, workspace_name)

        staging_root = await asyncio.to_thread(self._validated_staging_root)
        staging = Path(
            await asyncio.to_thread(
                tempfile.mkdtemp, prefix=f"pr-{task_id}-", dir=staging_root
            )
        )
        try:
            checkout = staging / "checkout"
            await self._run(
                "git",
                "clone",
                "--no-checkout",
                "--no-tags",
                "--single-branch",
                "--origin",
                "upstream",
                "--",
                repository_url,
                str(checkout),
                cwd=staging_root,
            )
            await self._run("git", "fetch", "--", "upstream", base_ref, cwd=checkout)
            resolved_base = await self._run("git", "rev-parse", "FETCH_HEAD^{commit}", cwd=checkout)
            if resolved_base.strip().lower() != base_sha.lower():
                raise RuntimeError("fetched base SHA does not match the requested base SHA")
            await self._run(
                "git",
                "fetch",
                "--",
                head_repository_url,
                head_ref,
                cwd=checkout,
            )
            resolved_head = (
                await self._run("git", "rev-parse", "FETCH_HEAD^{commit}", cwd=checkout)
            ).strip()
            if resolved_head.lower() != head_sha.lower():
                raise RuntimeError("fetched PR head SHA does not match the requested head SHA")
            await self._run("git", "checkout", "--detach", head_sha, cwd=checkout)
            # 跨文件系统搬运整个检出可能拷贝数 GB，同样不能阻塞事件循环。
            await asyncio.to_thread(shutil.move, str(checkout), str(workspace))
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, ignore_errors=True)

        workspace.chmod(0o700)
        return workspace

    async def review_input(self, workspace: Path, base_sha: str, head_sha: str) -> ReviewInput:
        self._validate_revisions(base_sha, head_sha)
        resolved_head = (
            await self._run("git", "rev-parse", "HEAD^{commit}", cwd=workspace)
        ).strip()
        if resolved_head.lower() != head_sha.lower():
            raise RuntimeError("PR review checkout revision does not match")
        try:
            await self._run("git", "cat-file", "-e", f"{base_sha}^{{commit}}", cwd=workspace)
        except RuntimeError:
            raise RuntimeError("PR review base revision is unavailable") from None
        comparison_sha = (
            await self._run("git", "merge-base", base_sha, head_sha, cwd=workspace)
        ).strip()
        if _SHA.fullmatch(comparison_sha) is None:
            raise RuntimeError("PR review merge base is unavailable")
        await self._run(
            "git",
            "update-ref",
            f"refs/heads/{REVIEW_BASE}",
            comparison_sha,
            cwd=workspace,
        )
        comparison = ("--end-of-options", comparison_sha, head_sha, "--")
        unified_diff = await self._run(
            "git",
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--full-index",
            "--find-renames",
            "--unified=5",
            *comparison,
            cwd=workspace,
        )
        parsed = self._parse_changed_ranges(unified_diff)
        return ReviewInput(
            ranges=ChangedRanges(
                parsed.ranges,
                comparison_sha=comparison_sha,
                changed_file_count=parsed.changed_file_count,
                additions=parsed.additions,
                deletions=parsed.deletions,
            ),
            unified_diff=unified_diff,
            review_base=REVIEW_BASE,
        )

    async def assert_pristine(
        self, workspace: Path, head_sha: str, comparison_sha: str
    ) -> None:
        self._validate_revisions(comparison_sha, head_sha)
        resolved_head = (
            await self._run("git", "rev-parse", "HEAD^{commit}", cwd=workspace)
        ).strip()
        resolved_base = (
            await self._run(
                "git",
                "rev-parse",
                f"refs/heads/{REVIEW_BASE}^{{commit}}",
                cwd=workspace,
            )
        ).strip()
        revisions_changed = (
            resolved_head.lower() != head_sha.lower()
            or resolved_base.lower() != comparison_sha.lower()
        )
        if revisions_changed:
            raise RuntimeError("PR review workspace revision changed during review")
        status = await self._run(
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            cwd=workspace,
        )
        if status:
            raise RuntimeError("PR review workspace was modified during review")

    def _validated_staging_root(self) -> Path:
        configured = Path(os.path.abspath(self.staging_root))
        self._reject_symlink_components(configured)
        configured.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._reject_symlink_components(configured)
        resolved = configured.resolve(strict=True)
        if resolved == self.workspace_root or resolved.is_relative_to(self.workspace_root):
            raise RuntimeError("PR staging root must be outside the analysis workspace root")
        if not resolved.is_dir():
            raise RuntimeError("PR staging root must be a directory")
        if hasattr(os, "getuid") and resolved.stat().st_uid != os.getuid():
            raise RuntimeError("PR staging root must be owned by the manager user")
        resolved.chmod(0o700)
        return resolved

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        for component in reversed([path, *path.parents]):
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode) or (
                hasattr(component, "is_junction") and component.is_junction()
            ):
                raise RuntimeError("PR staging root must not contain symlink components")

    def _remove_stale_workspace(self, workspace: Path, expected_name: str) -> None:
        try:
            resolved = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("unable to resolve stale PR workspace") from exc
        if resolved.parent != self.workspace_root or resolved.name != expected_name:
            raise RuntimeError("refusing to delete a path outside the workspace root")
        if not resolved.is_dir():
            raise RuntimeError("stale PR workspace is not a directory")
        shutil.rmtree(resolved, onexc=self._remove_readonly)

    @staticmethod
    def _remove_readonly(function, path: str, _error: BaseException) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    @staticmethod
    def _validate_prepare_inputs(
        task_id: int,
        repository_url: str,
        pr_number: int,
        base_ref: str,
        base_sha: str,
        head_sha: str,
        head_ref: str,
        head_repository_url: str,
    ) -> None:
        for field, value in (("task_id", task_id), ("pr_number", pr_number)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if (
            not isinstance(repository_url, str)
            or not repository_url
            or repository_url.startswith("-")
        ):
            raise ValueError("repository_url must not be a Git option")
        if not PRWorkspace._is_safe_ref(base_ref):
            raise ValueError("base_ref must be a single safe Git ref")
        if not PRWorkspace._is_safe_ref(head_ref):
            raise ValueError("head_ref must be a single safe Git ref")
        PRWorkspace._validate_head_repository_url(head_repository_url)
        PRWorkspace._validate_revisions(base_sha, head_sha)

    @staticmethod
    def _validate_revisions(base_sha: str, head_sha: str) -> None:
        for field, value in (("base_sha", base_sha), ("head_sha", head_sha)):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise ValueError(f"{field} must be a 40-character hexadecimal SHA")

    @staticmethod
    def _is_safe_ref(base_ref: str) -> bool:
        if (
            not isinstance(base_ref, str)
            or not base_ref
            or base_ref.startswith(("-", "+", "/"))
            or base_ref.endswith((".", "/"))
            or base_ref == "@"
            or ".." in base_ref
            or "@{" in base_ref
            or _INVALID_REF_CHARACTER.search(base_ref) is not None
        ):
            return False
        return all(
            component and not component.startswith(".") and not component.endswith(".lock")
            for component in base_ref.split("/")
        )

    @staticmethod
    def _validate_head_repository_url(url: str) -> None:
        if not isinstance(url, str) or not url or url.startswith("-"):
            raise ValueError("head_repository_url must be a safe Git URL")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ValueError("head_repository_url must be a safe Git URL") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"github.com", "gitcode.com"}
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
            or "%" in url
            or "\\" in url
        ):
            raise ValueError("head_repository_url must be a safe Git URL")
        candidate = url.removesuffix(".git")
        try:
            repository = parse_repository_url(candidate)
        except InvalidProviderUrl:
            raise ValueError("head_repository_url must be a safe Git URL") from None
        if url not in {repository.canonical_url, f"{repository.canonical_url}.git"}:
            raise ValueError("head_repository_url must be a safe Git URL")

    @staticmethod
    def _parse_changed_ranges(diff: str) -> ChangedRanges:
        ranges: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        changed_file_count = 0
        additions = 0
        deletions = 0
        old_path: str | None = None
        new_path: str | None = None
        old_line = 0
        new_line = 0
        state = "outside"

        def record(path: str | None, side: str, line_number: int) -> None:
            if path is None or line_number < 1:
                return
            key = (path, side)
            if ranges[key] and line_number <= ranges[key][-1][1] + 1:
                start, end = ranges[key][-1]
                ranges[key][-1] = (start, max(end, line_number))
            else:
                ranges[key].append((line_number, line_number))

        for line in diff.splitlines():
            if line.startswith("diff --git "):
                changed_file_count += 1
                old_path = None
                new_path = None
                state = "header"
                continue
            if state == "header" and line.startswith("--- "):
                old_path = PRWorkspace._parse_diff_path(line[4:], "a/")
                state = "old_path"
                continue
            if state == "old_path" and line.startswith("+++ "):
                new_path = PRWorkspace._parse_diff_path(line[4:], "b/")
                state = "ready"
                continue
            match = _HUNK_HEADER.match(line)
            if match is not None and state in {"ready", "hunk"}:
                old_line = int(match.group(1))
                new_line = int(match.group(3))
                state = "hunk"
                continue
            if state != "hunk" or not line:
                continue
            if line.startswith("-"):
                record(old_path, "LEFT", old_line)
                old_line += 1
                deletions += 1
            elif line.startswith("+"):
                record(new_path, "RIGHT", new_line)
                new_line += 1
                additions += 1
            elif line.startswith(" "):
                old_line += 1
                new_line += 1

        return ChangedRanges(
            {key: tuple(value) for key, value in ranges.items()},
            changed_file_count=changed_file_count,
            additions=additions,
            deletions=deletions,
        )

    @staticmethod
    def _parse_diff_path(value: str, prefix: str) -> str | None:
        path = PRWorkspace._decode_git_path(value)
        if path is None or path == "/dev/null":
            return None
        if not path.startswith(prefix):
            return None
        return normalize_repository_path(path.removeprefix(prefix))

    @staticmethod
    def _decode_git_path(value: str) -> str | None:
        if not value.startswith('"'):
            return value
        if len(value) < 2 or not value.endswith('"'):
            return None
        escaped = value[1:-1]
        decoded = bytearray()
        index = 0
        escapes = {
            '"': b'"',
            "\\": b"\\",
            "a": b"\a",
            "b": b"\b",
            "f": b"\f",
            "n": b"\n",
            "r": b"\r",
            "t": b"\t",
            "v": b"\v",
        }
        while index < len(escaped):
            character = escaped[index]
            if character != "\\":
                decoded.extend(character.encode("utf-8"))
                index += 1
                continue
            index += 1
            if index == len(escaped):
                return None
            character = escaped[index]
            if character in escapes:
                decoded.extend(escapes[character])
                index += 1
                continue
            if character not in "01234567":
                return None
            end = index
            while end < len(escaped) and end < index + 3 and escaped[end] in "01234567":
                end += 1
            decoded.append(int(escaped[index:end], 8))
            index = end
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _git_environment(isolated_home: Path) -> Mapping[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _SYSTEM_ENVIRONMENT_KEYS
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

    def _timeout_for(self, command: tuple[str, ...]) -> float:
        """clone/fetch/checkout 涉及大体量网络传输或落盘，用独立的宽松超时。"""
        if len(command) > 1 and command[1] in {"clone", "fetch", "checkout"}:
            return self.transfer_timeout_seconds
        return self.command_timeout_seconds

    async def _run(self, *command: str, cwd: Path) -> str:
        if not command or command[0] != "git":
            raise ValueError("PRWorkspace only permits Git commands")
        with tempfile.TemporaryDirectory(prefix="coderus-pr-git-") as isolated:
            isolated_home = Path(isolated)
            hooks_path = isolated_home / "hooks"
            hooks_path.mkdir(mode=0o700)
            hardened = (
                "git",
                "-c",
                f"core.hooksPath={hooks_path}",
                "-c",
                "core.fsmonitor=false",
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
                    env=self._git_environment(isolated_home),
                    timeout_seconds=self._timeout_for(command),
                    watch_path=watch_path,
                    max_path_bytes=self.max_workspace_bytes,
                )
                if await asyncio.to_thread(
                    path_size_exceeds, watch_path, self.max_workspace_bytes
                ):
                    raise CommandResourceLimitExceeded("command path size limit exceeded")
            except CommandTimedOut:
                raise RuntimeError("Git command timed out") from None
            except CommandResourceLimitExceeded:
                raise RuntimeError("PR workspace size limit exceeded") from None
        if result.returncode != 0:
            raise RuntimeError("git command failed")
        return result.stdout.decode("utf-8", errors="replace")
