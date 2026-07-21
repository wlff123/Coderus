from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from coderus.processes import run_process_sync

from .errors import GitPushError, InvalidPublisherInput
from .models import GitCommandResult

_GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_INVALID_REF_CHARACTERS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SHA_PATTERN = re.compile(r"[0-9A-Fa-f]{40}\Z")
_ASKPASS_SOURCE = """import os
import sys

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
name = "CODERUS_GIT_USERNAME" if "username" in prompt.casefold() else "CODERUS_GIT_TOKEN"
value = os.environ.get(name)
if value is None:
    raise SystemExit(1)
sys.stdout.write(value + "\\n")
"""


class GitRunner(Protocol):
    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> GitCommandResult: ...


class SubprocessGitRunner:
    def __init__(self, *, timeout_seconds: float = 120) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> GitCommandResult:
        completed = run_process_sync(
            args,
            cwd=cwd,
            env=env,
            timeout_seconds=self._timeout_seconds,
        )
        return GitCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout.decode("utf-8", errors="replace"),
            stderr=completed.stderr.decode("utf-8", errors="replace"),
        )


class HttpsGitPusher:
    def __init__(
        self,
        token: str,
        username: str,
        git_runner: GitRunner | None = None,
        git_timeout_seconds: float = 120,
    ) -> None:
        if not isinstance(token, str) or not token:
            raise InvalidPublisherInput("git token must not be empty")
        if not isinstance(username, str) or _USERNAME.fullmatch(username) is None:
            raise InvalidPublisherInput("git username is invalid")
        self._token = token
        self._username = username
        self._git_runner = (
            git_runner
            if git_runner is not None
            else SubprocessGitRunner(timeout_seconds=git_timeout_seconds)
        )

    def push(self, workspace: Path, remote_url: str, branch: str) -> None:
        workspace = Path(workspace)
        if not workspace.is_dir():
            raise InvalidPublisherInput("publish workspace must be an existing directory")
        self._validate_remote_url(remote_url)
        self._validate_branch(branch)

        commit, objects = self._resolve_source(workspace, branch)

        with tempfile.TemporaryDirectory(prefix="coderus-publish-") as temp_dir:
            temp_root = Path(temp_dir)
            temp_root.chmod(0o700)
            home = temp_root / "home"
            home.mkdir(mode=0o700)
            askpass = self._write_askpass(temp_root)
            git_dir = self._create_bare_git_dir(temp_root, objects)
            ref = f"refs/heads/{branch}"
            try:
                result = self._git_runner.run(
                    (
                        "git",
                        f"--git-dir={git_dir}",
                        "push",
                        "--",
                        remote_url,
                        f"{commit}:{ref}",
                    ),
                    cwd=temp_root,
                    env=self._git_environment(home, askpass),
                )
            except Exception:
                raise GitPushError("git push failed") from None
            if result.returncode != 0:
                raise GitPushError(f"git push failed with exit code {result.returncode}")

    def _resolve_source(self, workspace: Path, branch: str) -> tuple[str, Path]:
        env = self._inspection_environment()
        try:
            commit_result = self._git_runner.run(
                (
                    "git",
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{branch}^{{commit}}",
                ),
                cwd=workspace,
                env=env,
            )
            objects_result = self._git_runner.run(
                (
                    "git",
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    "objects",
                ),
                cwd=workspace,
                env=env,
            )
        except Exception:
            raise GitPushError("git source preparation failed") from None
        commit = commit_result.stdout.strip()
        objects_text = objects_result.stdout.strip()
        if (
            commit_result.returncode != 0
            or _SHA_PATTERN.fullmatch(commit) is None
            or objects_result.returncode != 0
            or not objects_text
        ):
            raise GitPushError("git source preparation failed")
        objects = Path(objects_text)
        if not objects.is_absolute() or not objects.is_dir():
            raise GitPushError("git source preparation failed")
        return commit.lower(), objects.resolve()

    @staticmethod
    def _create_bare_git_dir(temp_root: Path, objects: Path) -> Path:
        git_dir = temp_root / "repository.git"
        (git_dir / "objects" / "info").mkdir(parents=True, mode=0o700)
        (git_dir / "refs" / "heads").mkdir(parents=True, mode=0o700)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (git_dir / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
            encoding="ascii",
        )
        (git_dir / "objects" / "info" / "alternates").write_text(
            f"{objects.as_posix()}\n",
            encoding="utf-8",
        )
        return git_dir

    @staticmethod
    def _write_askpass(temp_root: Path) -> Path:
        helper = temp_root / "askpass.py"
        helper.write_text(_ASKPASS_SOURCE, encoding="utf-8")
        helper.chmod(0o700)
        executable = Path(sys.executable)
        if os.name == "nt":
            launcher = temp_root / "askpass.cmd"
            launcher.write_text(
                "@echo off\r\n"
                f'"{executable}" "{helper}" "%~1"\r\n',
                encoding="utf-8",
            )
        else:
            launcher = temp_root / "askpass.sh"
            launcher.write_text(
                "#!/bin/sh\n"
                f"exec {shlex.quote(str(executable))} {shlex.quote(str(helper))} \"$1\"\n",
                encoding="utf-8",
            )
        launcher.chmod(0o700)
        return launcher

    @staticmethod
    def _base_environment() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP", "LANG"}
        return {key: value for key, value in os.environ.items() if key in allowed}

    def _inspection_environment(self) -> dict[str, str]:
        env = self._base_environment()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
        )
        return env

    def _git_environment(self, home: Path, askpass: Path) -> dict[str, str]:
        env = self._base_environment()
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home),
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": os.devnull,
                "GIT_CONFIG_KEY_1": "credential.helper",
                "GIT_CONFIG_VALUE_1": "",
                "GCM_INTERACTIVE": "Never",
                "CODERUS_GIT_TOKEN": self._token,
                "CODERUS_GIT_USERNAME": self._username,
            }
        )
        return env

    @staticmethod
    def _validate_remote_url(url: str) -> None:
        if (
            not isinstance(url, str)
            or not url
            or url != url.strip()
            or "%" in url
            or "\\" in url
            or any(character.isspace() or ord(character) < 32 for character in url)
        ):
            raise InvalidPublisherInput("git remote URL is invalid")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise InvalidPublisherInput("git remote URL is invalid") from None
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or host not in {"github.com", "gitcode.com"}
            or parsed.netloc.lower() != host
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidPublisherInput("git remote URL is invalid")
        parts = parsed.path.split("/")[1:]
        if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
            raise InvalidPublisherInput("git remote URL is invalid")
        owner, repository = parts
        if not repository.endswith(".git"):
            raise InvalidPublisherInput("git remote URL is invalid")
        repository = repository[:-4]
        owner_pattern = _GITHUB_OWNER if host == "github.com" else _REPOSITORY_NAME
        if owner_pattern.fullmatch(owner) is None or _REPOSITORY_NAME.fullmatch(repository) is None:
            raise InvalidPublisherInput("git remote URL is invalid")

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if (
            not isinstance(branch, str)
            or not branch
            or branch.startswith(("-", ".", "/"))
            or branch.endswith((".", "/"))
            or ".." in branch
            or "//" in branch
            or "@{" in branch
            or _INVALID_REF_CHARACTERS.search(branch)
        ):
            raise InvalidPublisherInput("branch contains unsafe characters")
        for component in branch.split("/"):
            if not component or component.startswith(".") or component.endswith(".lock"):
                raise InvalidPublisherInput("branch contains unsafe characters")
