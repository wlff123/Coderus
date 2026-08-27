import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from coderus.forge import (
    GitCommandResult,
    GitPushError,
    HttpsGitPusher,
    InvalidPublisherInput,
)


class RecordingGitRunner:
    def __init__(self, result: GitCommandResult | None = None) -> None:
        self.result = result or GitCommandResult(returncode=0)
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> GitCommandResult:
        if "push" not in args:
            if "--verify" in args:
                return GitCommandResult(returncode=0, stdout=f"{'a' * 40}\n")
            objects = cwd / ".git" / "objects"
            objects.mkdir(parents=True, exist_ok=True)
            return GitCommandResult(returncode=0, stdout=f"{objects.resolve()}\n")
        askpass = Path(env["GIT_ASKPASS"])
        helper = askpass.with_name("askpass.py")
        self.calls.append(
            {
                "args": args,
                "cwd": cwd,
                "env": dict(env),
                "askpass_path": askpass,
                "askpass_text": askpass.read_text(encoding="utf-8"),
                "helper_text": helper.read_text(encoding="utf-8"),
                "askpass_mode": stat.S_IMODE(askpass.stat().st_mode),
            }
        )
        return self.result


def run_git(
    workspace: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=workspace,
        env=env,
        input=None,
        capture_output=True,
        text=True,
        check=True,
    )


def initialize_repository(workspace: Path, branch: str = "coderus/task-1") -> None:
    workspace.mkdir()
    run_git(workspace, "init", "--quiet")
    run_git(workspace, "config", "user.name", "Coderus Test")
    run_git(workspace, "config", "user.email", "coderus@example.com")
    (workspace / "README.md").write_text("test\n", encoding="utf-8")
    run_git(workspace, "add", "README.md")
    run_git(workspace, "commit", "--quiet", "-m", "test")
    run_git(workspace, "branch", "-M", branch)


class RealGitSecurityRunner:
    def __init__(self) -> None:
        self.resolved_urls: list[str] = []
        self.credentials: list[dict[str, str]] = []
        self.injected_marker_exists = False
        self.failures: list[str] = []

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> GitCommandResult:
        if "push" not in args:
            completed = subprocess.run(
                args,
                cwd=cwd,
                env=dict(env),
                capture_output=True,
                text=True,
                check=False,
            )
            return GitCommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        push_index = args.index("push")
        remote_url = args[-2]
        resolved = subprocess.run(
            (*args[:push_index], "ls-remote", "--get-url", remote_url),
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode != 0:
            self.failures.append(f"url resolution failed: {resolved.stderr}")
            return GitCommandResult(returncode=0)
        self.resolved_urls.append(resolved.stdout.strip())

        credential = subprocess.run(
            ("git", "credential", "fill"),
            cwd=cwd,
            env=dict(env),
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if credential.returncode != 0:
            self.failures.append(f"credential fill failed: {credential.stderr}")
            return GitCommandResult(returncode=0)
        self.credentials.append(
            dict(
                line.split("=", 1)
                for line in credential.stdout.splitlines()
                if "=" in line
            )
        )
        self.injected_marker_exists = (cwd / "askpass-injected.txt").exists()
        return GitCommandResult(returncode=0)


def test_token_push_does_not_read_source_local_worktree_global_or_system_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    initialize_repository(workspace)
    malicious_url = "https://evil.invalid/intercept/"
    run_git(
        workspace,
        "config",
        f"url.{malicious_url}.insteadOf",
        "https://github.com/",
    )
    run_git(workspace, "config", "extensions.worktreeConfig", "true")
    run_git(
        workspace,
        "config",
        "--worktree",
        f"url.{malicious_url}.pushInsteadOf",
        "https://github.com/",
    )
    hostile_home = tmp_path / "hostile-home"
    hostile_home.mkdir()
    (hostile_home / ".gitconfig").write_text(
        f'[url "{malicious_url}"]\n\tinsteadOf = https://github.com/\n',
        encoding="utf-8",
    )
    hostile_system = tmp_path / "hostile-system.config"
    hostile_system.write_text(
        f'[url "{malicious_url}"]\n\tpushInsteadOf = https://github.com/\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(hostile_home))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_system))
    runner = RealGitSecurityRunner()

    HttpsGitPusher("secret", "x-access-token", git_runner=runner).push(
        workspace,
        "https://github.com/coderus-bot/widgets.git",
        "coderus/task-1",
    )

    assert runner.resolved_urls == ["https://github.com/coderus-bot/widgets.git"]
    assert runner.failures == []


def test_real_git_askpass_preserves_shell_metacharacters_without_execution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_repository(workspace)
    token = "sp ace&|()<>^%!safe&echo injected>askpass-injected.txt"
    runner = RealGitSecurityRunner()

    HttpsGitPusher(token, "x-access-token", git_runner=runner).push(
        workspace,
        "https://github.com/coderus-bot/widgets.git",
        "coderus/task-1",
    )

    assert runner.credentials == [
        {
            "protocol": "https",
            "host": "github.com",
            "username": "x-access-token",
            "password": token,
        }
    ]
    assert runner.injected_marker_exists is False
    assert runner.failures == []


@pytest.mark.parametrize(
    ("remote_url", "username"),
    [
        ("https://github.com/coderus-bot/widgets.git", "x-access-token"),
        ("https://gitcode.com/coderus-bot/widgets.git", "coderus-bot"),
    ],
)
def test_push_uses_ephemeral_askpass_and_isolated_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_url: str,
    username: str,
) -> None:
    token = "transport-secret-token"
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-git")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "unsafe-system-config")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "unsafe-global-config")
    runner = RecordingGitRunner()

    HttpsGitPusher(token, username, git_runner=runner).push(
        tmp_path, remote_url, "coderus/issue-7-11"
    )

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["args"][:1] == ("git",)
    assert call["args"][1].startswith("--git-dir=")
    assert call["args"][2:] == (
        "push",
        "--",
        remote_url,
        f"{'a' * 40}:refs/heads/coderus/issue-7-11",
    )
    assert call["cwd"] != tmp_path
    assert token not in repr(call["args"])
    assert username not in call["askpass_text"]
    assert token not in call["askpass_text"]
    assert token not in call["helper_text"]
    if os.name != "nt":
        assert call["askpass_mode"] == 0o700

    env = call["env"]
    assert env["CODERUS_GIT_TOKEN"] == token
    assert env["CODERUS_GIT_USERNAME"] == username
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert env["GIT_CONFIG_VALUE_0"] == os.devnull
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_1"] == ""
    assert env["GCM_INTERACTIVE"] == "Never"
    assert env["HOME"] == env["XDG_CONFIG_HOME"]
    assert "UNRELATED_SECRET" not in env
    assert "GIT_CONFIG_SYSTEM" not in env
    assert not Path(env["HOME"]).exists()
    assert not call["askpass_path"].exists()


@pytest.mark.parametrize(
    "remote_url",
    [
        "http://github.com/acme/widgets.git",
        "git@github.com:acme/widgets.git",
        "ssh://git@github.com/acme/widgets.git",
        "https://token@github.com/acme/widgets.git",
        "https://user:password@gitcode.com/acme/widgets.git",
        "https://github.com:443/acme/widgets.git",
        "https://github.com/acme/../widgets.git",
        "https://github.com/acme/widgets.git?token=secret",
        "https://gitcode.com/acme/widgets.git#fragment",
        "https://example.com/acme/widgets.git",
        "https://github.com/acme/widgets",
        "https://github.com/acme/nested/widgets.git",
    ],
)
def test_push_rejects_noncanonical_or_credential_bearing_remote(
    tmp_path: Path, remote_url: str
) -> None:
    runner = RecordingGitRunner()

    with pytest.raises(InvalidPublisherInput, match="remote"):
        HttpsGitPusher("secret", "bot", git_runner=runner).push(
            tmp_path, remote_url, "coderus/task-1"
        )

    assert runner.calls == []


@pytest.mark.parametrize("branch", ["", "--force", "../main", "feature:main", "a b"])
def test_push_rejects_unsafe_branch_before_git(tmp_path: Path, branch: str) -> None:
    runner = RecordingGitRunner()

    with pytest.raises(InvalidPublisherInput, match="branch"):
        HttpsGitPusher("secret", "bot", git_runner=runner).push(
            tmp_path, "https://gitcode.com/bot/widgets.git", branch
        )

    assert runner.calls == []


def test_push_failure_does_not_expose_token_or_git_output(tmp_path: Path) -> None:
    token = "transport-secret-token"
    runner = RecordingGitRunner(
        GitCommandResult(returncode=128, stderr=f"authentication failed for {token}")
    )

    with pytest.raises(GitPushError) as error:
        HttpsGitPusher(token, "bot", git_runner=runner).push(
            tmp_path, "https://gitcode.com/bot/widgets.git", "coderus/task-1"
        )

    assert str(error.value) == "git push failed with exit code 128"
    assert token not in repr(error.value)
    assert error.value.__cause__ is None


def test_push_wraps_runner_exception_without_exposing_token(tmp_path: Path) -> None:
    token = "transport-secret-token"

    class ExplodingRunner:
        def run(
            self,
            args: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str],
        ) -> GitCommandResult:
            if "push" not in args:
                if "--verify" in args:
                    return GitCommandResult(returncode=0, stdout=f"{'a' * 40}\n")
                objects = cwd / ".git" / "objects"
                objects.mkdir(parents=True, exist_ok=True)
                return GitCommandResult(returncode=0, stdout=f"{objects.resolve()}\n")
            raise OSError(f"environment contained {token}")

    with pytest.raises(GitPushError) as error:
        HttpsGitPusher(token, "bot", git_runner=ExplodingRunner()).push(
            tmp_path, "https://gitcode.com/bot/widgets.git", "coderus/task-1"
        )

    assert str(error.value) == "git push failed"
    assert token not in repr(error.value)
    assert error.value.__cause__ is None
