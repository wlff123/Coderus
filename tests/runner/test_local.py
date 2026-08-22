from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import coderus.runner.local as local_runner_module
from coderus.runner import (
    AgentRole,
    JobResult,
    JobSpec,
    JobStatus,
    LocalCodexRunner,
    RetryableAgentError,
    RunnerConfig,
    Stage,
)

FAKE_CLI = """\
import json
import os
import subprocess
import sys
import time

stdin = sys.stdin.read()
if "review" in sys.argv:
    print(json.dumps({"argv": sys.argv[1:], "stdin": stdin}))
    raise SystemExit
prompt = stdin if sys.argv[-1] == "-" else sys.argv[-1]
if prompt == "show-env":
    print(json.dumps(dict(os.environ), sort_keys=True))
elif prompt == "show-runtime":
    codex_home = os.environ["CODEX_HOME"]
    print(json.dumps({
        "environment": dict(os.environ),
        "codex_home_entries": sorted(os.listdir(codex_home)),
    }, sort_keys=True))
elif prompt == "probe-boundary":
    with open("probe.json", encoding="utf-8") as probe_file:
        paths = json.load(probe_file)
    results = {}
    for label, path in paths.items():
        try:
            with open(path, encoding="utf-8") as candidate:
                candidate.read()
        except PermissionError:
            results[label] = "denied"
        except FileNotFoundError:
            results[label] = "missing"
        else:
            results[label] = "readable"
    with open("workspace-write.txt", "w", encoding="utf-8") as output:
        output.write("workspace")
    temp_path = os.path.join(os.environ["TMPDIR"], "temp-write.txt")
    with open(temp_path, "w", encoding="utf-8") as output:
        output.write("temp")
    results["workspace_write"] = os.path.exists("workspace-write.txt")
    results["temp_write"] = os.path.exists(temp_path)
    print(json.dumps(results, sort_keys=True))
elif prompt == "probe-execute":
    workspace_tool = os.path.abspath("workspace-tool.sh")
    temp_tool = os.path.join(os.environ["TMPDIR"], "temp-tool.sh")
    alias_root = os.path.join(os.environ["CODEX_HOME"], "tmp", "arg0")
    os.makedirs(alias_root, exist_ok=True)
    alias_session = os.path.join(alias_root, f"codex-arg0-{os.getpid()}")
    os.mkdir(alias_session)
    lock_file = open(os.path.join(alias_session, ".lock"), "a+", encoding="utf-8")
    if sys.platform == "linux":
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.symlink(sys.executable, os.path.join(alias_session, "apply_patch"))
    alias_tool = os.path.join(alias_session, "alias-tool.sh")
    with open(temp_tool, "w", encoding="utf-8") as tool:
        tool.write("#!/bin/sh\\nexit 0\\n")
    os.chmod(temp_tool, 0o700)
    with open(alias_tool, "w", encoding="utf-8") as tool:
        tool.write("#!/bin/sh\\nexit 0\\n")
    os.chmod(alias_tool, 0o700)
    results = {}
    for label, path in {
        "workspace": workspace_tool,
        "run_root": temp_tool,
        "codex_alias": alias_tool,
    }.items():
        try:
            completed = subprocess.run(path, check=False)
        except PermissionError:
            results[label] = "denied"
        else:
            results[label] = f"executed:{completed.returncode}"
    print(json.dumps(results, sort_keys=True))
elif prompt == "probe-git":
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
    )
    print(json.dumps({
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }, sort_keys=True))
elif prompt == "exit-seven":
    print("failed", file=sys.stderr)
    raise SystemExit(7)
elif prompt == "sleep":
    time.sleep(30)
elif prompt == "large-output":
    sys.stdout.write("stdout-prefix-" + "a" * 100 + "-stdout-tail")
    sys.stderr.write("stderr-prefix-" + "b" * 100 + "-stderr-tail")
elif prompt == "large-workspace":
    with open("large-workspace.bin", "wb") as output:
        output.write(b"x" * 4096)
        output.flush()
    time.sleep(30)
elif prompt == "large-native-review":
    sys.stderr.write("native-review-" + "n" * 500_000)
else:
    print(json.dumps({"argv": sys.argv[1:], "prompt": prompt}))
    print("diagnostic", file=sys.stderr)
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> tuple[str, ...]:
    return (sys.executable, "-c", FAKE_CLI)


def make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspaces"
    workspace = root / "task-1"
    workspace.mkdir(parents=True)
    return root, workspace


def make_spec(workspace: Path, prompt: str, **changes: object) -> JobSpec:
    values: dict[str, object] = {
        "job_id": "run-1",
        "stage": Stage.DEVELOP,
        "role": AgentRole.DEVELOPER,
        "workspace": workspace,
        "prompt": prompt,
    }
    values.update(changes)
    if values["stage"] is Stage.PR_REVIEW:
        values.setdefault("review_base", "coderus-review-base")
    return JobSpec(**values)


def test_command_runtime_paths_cover_symlinked_openai_install(tmp_path: Path) -> None:
    openai_root = tmp_path / "lib" / "node_modules" / "@openai"
    target = openai_root / "codex" / "bin" / "codex.js"
    target.parent.mkdir(parents=True)
    target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    target.chmod(0o700)
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    try:
        executable.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    paths = local_runner_module._command_runtime_paths((str(executable),))

    assert executable.parent.resolve() in paths
    assert target.resolve() in paths
    assert openai_root.resolve() in paths


@pytest.mark.skipif(os.name != "posix", reason="POSIX command resolution only")
def test_resolve_codex_command_uses_node_for_npm_symlink(tmp_path: Path) -> None:
    target = tmp_path / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    target.parent.mkdir(parents=True)
    target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    target.chmod(0o700)
    executable = tmp_path / "bin" / "codex"
    node = executable.parent / "node"
    executable.parent.mkdir()
    executable.symlink_to(target)
    node.write_bytes(b"node")
    node.chmod(0o700)

    command = local_runner_module.resolve_codex_command(str(executable))

    assert command == (str(node.resolve()), str(target.resolve()))


@pytest.mark.skipif(os.name != "posix", reason="POSIX command resolution only")
def test_resolve_codex_command_rejects_missing_posix_binary(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Codex executable"):
        local_runner_module.resolve_codex_command(str(tmp_path / "missing-codex"))


@pytest.fixture
def review_spec(tmp_path: Path) -> JobSpec:
    _, workspace = make_workspace(tmp_path)
    schema = workspace / "review-schema.json"
    schema.write_text("{}", encoding="utf-8")
    return make_spec(
        workspace,
        "Review the current changes",
        stage=Stage.PR_REVIEW,
        role=AgentRole.PR_REVIEWER,
        proxy_token="short-lived-token",
        output_schema=schema,
        review_base="coderus-review-base",
    )


def test_build_command_uses_schema_driven_exec_for_pr_review(
    fake_cli: tuple[str, ...], review_spec: JobSpec
) -> None:
    root = review_spec.workspace.parent
    runner = LocalCodexRunner(
        RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1", model="test-model")
    )

    command = runner.build_command(review_spec, output_schema=review_spec.output_schema)

    assert command[: len(fake_cli)] == list(fake_cli)
    assert "exec" in command
    assert command[-1] == "请按开发者检视规范完成本次静态检视，并严格输出 Schema 对象。"
    assert "review" not in command
    assert "--base" not in command
    assert "developer_instructions=" in " ".join(command)
    assert "resume" not in command
    assert review_spec.prompt not in command
    assert "project_doc_max_bytes=0" in command
    assert 'model_provider="coderus_proxy"' in command
    assert 'model_providers.coderus_proxy.base_url="http://127.0.0.1:9999/v1"' in command
    assert 'model_providers.coderus_proxy.env_key="OPENAI_API_KEY"' in command
    assert 'model_providers.coderus_proxy.wire_api="responses"' in command
    assert "model_providers.coderus_proxy.supports_websockets=false" in command
    assert command[command.index("--model") + 1] == "test-model"
    assert command[command.index("--output-schema") + 1] == str(review_spec.output_schema)
    assert "--search" not in command
    expected_sandbox = "danger-full-access" if sys.platform == "linux" else "read-only"
    assert command[command.index("--sandbox") + 1] == expected_sandbox


def test_pr_review_never_inherits_configured_danger_full_access_mode(
    fake_cli: tuple[str, ...], review_spec: JobSpec
) -> None:
    runner = LocalCodexRunner(
        RunnerConfig(
            review_spec.workspace.parent,
            fake_cli,
            "http://127.0.0.1:9999/v1",
            sandbox_mode="danger-full-access",
        )
    )

    command = runner.build_command(review_spec, output_schema=review_spec.output_schema)

    assert command[: len(fake_cli)] == list(fake_cli)
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    expected = "danger-full-access" if sys.platform == "linux" else "read-only"
    assert command[command.index("--sandbox") + 1] == expected


def test_linux_pr_review_uses_landlock_instead_of_the_unavailable_inner_sandbox(
    fake_cli: tuple[str, ...], review_spec: JobSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_runner_module.sys, "platform", "linux")
    runner = LocalCodexRunner(RunnerConfig(review_spec.workspace.parent, fake_cli))

    command = runner.build_command(review_spec, output_schema=review_spec.output_schema)

    assert command[command.index("--sandbox") + 1] == "danger-full-access"
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


@pytest.mark.asyncio
async def test_pr_review_runs_without_stdin_and_with_output_schema(
    fake_cli: tuple[str, ...], review_spec: JobSpec
) -> None:
    runner = LocalCodexRunner(
        RunnerConfig(
            review_spec.workspace.parent,
            fake_cli,
            "http://127.0.0.1:9999/v1",
            model="test-model",
        )
    )

    result = await runner.run(review_spec)

    payload = json.loads(result.stdout)
    assert result.status is JobStatus.SUCCEEDED
    assert "exec" in payload["argv"]
    assert 'model_provider="coderus_proxy"' in payload["argv"]
    assert payload["argv"][-1] == "请按开发者检视规范完成本次静态检视，并严格输出 Schema 对象。"
    assert payload["prompt"] == payload["argv"][-1]
    assert "stdin" not in payload
    assert "review" not in payload["argv"]
    assert "--base" not in payload["argv"]
    assert "--output-schema" in payload["argv"]


@pytest.mark.asyncio
async def test_pr_review_passes_no_stdin_text_to_the_process(
    fake_cli: tuple[str, ...], review_spec: JobSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = LocalCodexRunner(RunnerConfig(review_spec.workspace.parent, fake_cli))
    stdin_texts: list[str | None] = []

    async def capture_process(*args, stdin_text: str | None = None, **kwargs) -> JobResult:
        del args, kwargs
        stdin_texts.append(stdin_text)
        return JobResult(
            job_id=review_spec.job_id,
            status=JobStatus.SUCCEEDED,
            exit_code=0,
            stdout="",
            stderr="",
            output_truncated=False,
            duration_seconds=0,
        )

    monkeypatch.setattr(runner, "_run_process", capture_process)

    result = await runner.run(review_spec)

    assert result.status is JobStatus.SUCCEEDED
    assert stdin_texts == [None]


def test_pr_review_adds_large_prompt_to_developer_instructions(
    fake_cli: tuple[str, ...], review_spec: JobSpec
) -> None:
    runner = LocalCodexRunner(RunnerConfig(review_spec.workspace.parent, fake_cli))
    prompt = "review-context-" + "n" * 500_000
    spec = JobSpec(
        job_id=review_spec.job_id,
        stage=review_spec.stage,
        role=review_spec.role,
        workspace=review_spec.workspace,
        prompt=prompt,
        output_schema=review_spec.output_schema,
        review_base="coderus-review-base",
        max_output_bytes=2_000_000,
    )

    command = runner.build_command(spec, output_schema=spec.output_schema)

    assert f"developer_instructions={json.dumps(prompt)}" in command
    assert command[-1] == "请按开发者检视规范完成本次静态检视，并严格输出 Schema 对象。"
    assert "review" not in command
    assert "--base" not in command


def test_build_command_uses_separate_arguments_and_role_sandbox(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(
        RunnerConfig(
            workspace_root=root,
            codex_command=fake_cli,
            api_base_url="http://127.0.0.1:9999/v1",
        )
    )
    prompt = "inspect; exit 99"

    command = runner.build_command(make_spec(workspace, prompt))

    assert isinstance(command, list)
    assert command[: len(fake_cli)] == list(fake_cli)
    assert command[-1] == prompt
    assert "workspace-write" in command
    assert 'approval_policy="never"' in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--ephemeral" in command
    if os.name == "nt":
        assert 'windows.sandbox="unelevated"' in command


def test_build_command_applies_model_and_network_policy(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(
        RunnerConfig(
            root,
            fake_cli,
            "http://127.0.0.1:9999/v1",
            model="test-model",
            network_access=True,
        )
    )

    command = runner.build_command(make_spec(workspace, "work", proxy_token="short-lived-token"))

    assert "sandbox_workspace_write.network_access=true" in command
    assert 'model_provider="coderus_proxy"' in command
    assert 'model_providers.coderus_proxy.base_url="http://127.0.0.1:9999/v1"' in command
    assert 'model_providers.coderus_proxy.env_key="OPENAI_API_KEY"' in command
    assert 'model_providers.coderus_proxy.wire_api="responses"' in command
    assert "model_providers.coderus_proxy.supports_websockets=false" in command
    assert command[command.index("--model") + 1] == "test-model"


def test_build_command_preserves_codex_login_when_no_proxy_token(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "https://api.openai.com/v1"))

    command = runner.build_command(make_spec(workspace, "work"))

    assert not any(argument.startswith("model_provider=") for argument in command)
    assert not any(argument.startswith("model_providers.") for argument in command)


def test_build_command_uses_read_only_for_reviewers(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))
    spec = JobSpec(
        job_id="run-1",
        stage=Stage.REVIEW_CORRECTNESS,
        role=AgentRole.REVIEWER_A,
        workspace=workspace,
        prompt="Review",
    )

    assert "read-only" in runner.build_command(spec)


def test_build_command_can_disable_nested_codex_sandbox(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(
        RunnerConfig(
            root,
            fake_cli,
            "http://127.0.0.1:9999/v1",
            sandbox_mode="danger-full-access",
        )
    )
    reviewer = JobSpec(
        job_id="run-1",
        stage=Stage.REVIEW_CORRECTNESS,
        role=AgentRole.REVIEWER_A,
        workspace=workspace,
        prompt="Review",
    )

    assert "danger-full-access" in runner.build_command(make_spec(workspace, "work"))
    assert "danger-full-access" in runner.build_command(reviewer)


@pytest.mark.asyncio
async def test_run_returns_output_and_exit_code(tmp_path: Path, fake_cli: tuple[str, ...]) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))

    result = await runner.run(make_spec(workspace, "ok"))

    assert result.status is JobStatus.SUCCEEDED
    assert result.exit_code == 0
    assert json.loads(result.stdout)["prompt"] == "ok"
    assert result.stderr.splitlines() == ["diagnostic"]


@pytest.mark.asyncio
async def test_run_returns_nonzero_exit_code(tmp_path: Path, fake_cli: tuple[str, ...]) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))

    result = await runner.run(make_spec(workspace, "exit-seven"))

    assert result.status is JobStatus.FAILED
    assert result.exit_code == 7
    assert result.stderr.splitlines() == ["failed"]


@pytest.mark.asyncio
async def test_run_passes_only_allowlisted_environment_and_proxy_credentials(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    manager_environment = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "safe-locale",
        "OPENAI_API_KEY": "manager-real-key",
        "GITHUB_TOKEN": "manager-git-token",
        "FEISHU_APP_SECRET": "manager-feishu-secret",
        "MANAGER_DATABASE_URL": "sqlite:///manager.db",
    }
    runtime_root = tmp_path / "runtime"
    runner = LocalCodexRunner(
        RunnerConfig(
            workspace_root=root,
            codex_command=fake_cli,
            api_base_url="http://127.0.0.1:9999/v1",
            environment_allowlist=frozenset({"PATH", "LANG"}),
            runtime_root=runtime_root,
        ),
        manager_environment=manager_environment,
    )

    result = await runner.run(make_spec(workspace, "show-env", proxy_token="short-lived-token"))
    child_environment = json.loads(result.stdout)

    assert child_environment["LANG"] == "safe-locale"
    assert child_environment["OPENAI_BASE_URL"] == "http://127.0.0.1:9999/v1"
    assert child_environment["OPENAI_API_KEY"] == "short-lived-token"
    assert child_environment["PIP_REQUIRE_VIRTUALENV"] == "true"
    assert Path(child_environment["GIT_CONFIG_GLOBAL"]).name == ".gitconfig"
    assert child_environment["GIT_CONFIG_GLOBAL"] != os.devnull
    task_temp = Path(child_environment["TMPDIR"])
    assert child_environment["TEMP"] == str(task_temp)
    assert child_environment["TMP"] == str(task_temp)
    assert task_temp.parent.parent == runtime_root.resolve()
    assert workspace.resolve() not in task_temp.parents
    assert ".coderus" not in task_temp.parts
    assert not task_temp.exists()
    assert list(runtime_root.iterdir()) == []
    assert "GITHUB_TOKEN" not in child_environment
    assert "FEISHU_APP_SECRET" not in child_environment
    assert "MANAGER_DATABASE_URL" not in child_environment
    assert "manager-real-key" not in repr(runner)


@pytest.mark.asyncio
async def test_run_does_not_copy_implicit_manager_codex_auth(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    manager_home = tmp_path / "manager-home"
    manager_codex_home = manager_home / ".codex"
    manager_codex_home.mkdir(parents=True)
    (manager_codex_home / "auth.json").write_text('{"test":"auth"}', encoding="utf-8")
    (manager_codex_home / "config.toml").write_text("secret = true", encoding="utf-8")
    (manager_codex_home / "history.jsonl").write_text("private", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    runner = LocalCodexRunner(
        RunnerConfig(root, fake_cli, runtime_root=runtime_root),
        manager_environment={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(manager_home),
            "CODEX_HOME": str(manager_codex_home),
        },
    )

    result = await runner.run(make_spec(workspace, "show-runtime"))
    runtime = json.loads(result.stdout)
    environment = runtime["environment"]

    assert result.status is JobStatus.SUCCEEDED
    assert runtime["codex_home_entries"] == ["tmp"]
    assert environment["HOME"] != str(manager_home)
    assert environment["CODEX_HOME"] != str(manager_codex_home)
    assert Path(environment["HOME"]).parent.parent == runtime_root.resolve()
    assert Path(environment["CODEX_HOME"]).parent == Path(environment["HOME"]).parent
    assert not Path(environment["HOME"]).exists()


@pytest.mark.asyncio
async def test_proxy_backed_run_does_not_copy_manager_codex_auth(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    manager_codex_home = tmp_path / "manager-codex"
    manager_codex_home.mkdir()
    (manager_codex_home / "auth.json").write_text('{"test":"auth"}', encoding="utf-8")
    runner = LocalCodexRunner(
        RunnerConfig(root, fake_cli, runtime_root=tmp_path / "runtime"),
        manager_environment={
            "PATH": os.environ.get("PATH", ""),
            "CODEX_HOME": str(manager_codex_home),
        },
    )

    result = await runner.run(make_spec(workspace, "show-runtime", proxy_token="short-lived-token"))

    assert json.loads(result.stdout)["codex_home_entries"] == ["tmp"]


@pytest.mark.asyncio
async def test_run_rejects_runtime_root_inside_workspace_tree(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, runtime_root=root / "manager-runtime"))

    with pytest.raises(ValueError, match="runtime root"):
        await runner.run(make_spec(workspace, "ok"))


@pytest.mark.asyncio
async def test_run_rejects_nested_symlink_in_runtime_root(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    target = tmp_path / "runtime-target"
    target.mkdir()
    link = tmp_path / "runtime-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, runtime_root=link / "nested"))

    with pytest.raises(ValueError, match="symlink"):
        await runner.run(make_spec(workspace, "ok"))


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only")
async def test_linux_landlock_denies_manager_and_sibling_files_but_allows_run_paths(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    sibling = root / "task-2"
    sibling.mkdir()
    manager_home = tmp_path / "manager-home"
    manager_codex = manager_home / ".codex"
    manager_codex.mkdir(parents=True)
    paths = {
        "manager_secret": tmp_path / "secrets.env",
        "manager_db": tmp_path / "coderus.db",
        "sibling_workspace": sibling / "private.txt",
        "real_home": manager_home / "private.txt",
        "real_codex_home": manager_codex / "auth.json",
    }
    for label, path in paths.items():
        path.write_text(label, encoding="utf-8")
    (workspace / "probe.json").write_text(
        json.dumps({label: str(path) for label, path in paths.items()}),
        encoding="utf-8",
    )
    runner = LocalCodexRunner(
        RunnerConfig(root, fake_cli, runtime_root=tmp_path / "runtime"),
        manager_environment={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(manager_home),
            "CODEX_HOME": str(manager_codex),
        },
    )

    result = await runner.run(
        make_spec(workspace, "probe-boundary", proxy_token="short-lived-token")
    )
    observed = json.loads(result.stdout)

    assert result.status is JobStatus.SUCCEEDED
    assert {observed[label] for label in paths} == {"denied"}
    assert observed["workspace_write"] is True
    assert observed["temp_write"] is True


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only")
@pytest.mark.parametrize(
    ("stage", "role", "workspace_result"),
    [
        (Stage.PR_REVIEW, AgentRole.PR_REVIEWER, "denied"),
        (Stage.DEVELOP, AgentRole.DEVELOPER, "executed:0"),
    ],
)
async def test_linux_landlock_execute_is_limited_by_task_mode(
    tmp_path: Path,
    fake_cli: tuple[str, ...],
    stage: Stage,
    role: AgentRole,
    workspace_result: str,
) -> None:
    root, workspace = make_workspace(tmp_path)
    workspace_tool = workspace / "workspace-tool.sh"
    workspace_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    workspace_tool.chmod(0o700)
    config = RunnerConfig(root, fake_cli, runtime_root=tmp_path / "runtime")
    if stage is Stage.PR_REVIEW:

        class ReviewProbeRunner(LocalCodexRunner):
            def build_command(self, spec: JobSpec, *, output_schema=None) -> list[str]:
                return [*fake_cli, "probe-execute"]

            def _build_exec_command(self, spec: JobSpec, **kwargs) -> list[str]:
                return [*fake_cli, "probe-execute"]

        runner = ReviewProbeRunner(config)
    else:
        runner = LocalCodexRunner(config)

    result = await runner.run(
        make_spec(
            workspace,
            "probe-execute",
            stage=stage,
            role=role,
            proxy_token="short-lived-token",
        )
    )
    observed = json.loads(result.stdout)

    assert result.status is JobStatus.SUCCEEDED
    assert observed == {
        "codex_alias": "executed:0",
        "run_root": "denied",
        "workspace": workspace_result,
    }


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-only")
async def test_linux_landlock_allows_read_only_git_inspection(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    subprocess.run(("git", "init"), cwd=workspace, check=True, capture_output=True)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, runtime_root=tmp_path / "runtime"))
    spec = JobSpec(
        job_id="review-probe",
        stage=Stage.REVIEW_CORRECTNESS,
        role=AgentRole.REVIEWER_A,
        workspace=workspace,
        prompt="probe-git",
    )

    result = await runner.run(spec)
    assert result.status is JobStatus.SUCCEEDED, result.stderr
    observed = json.loads(result.stdout)

    assert observed == {"returncode": 0, "stdout": "true", "stderr": ""}


@pytest.mark.asyncio
async def test_run_does_not_inherit_manager_api_key_without_proxy_token(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(
        RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"),
        manager_environment={"OPENAI_API_KEY": "manager-real-key"},
    )

    result = await runner.run(make_spec(workspace, "show-env"))

    assert "OPENAI_API_KEY" not in json.loads(result.stdout)


@pytest.mark.asyncio
async def test_run_blocks_manager_api_key_even_if_allowlisted(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(
        RunnerConfig(
            root,
            fake_cli,
            "http://127.0.0.1:9999/v1",
            environment_allowlist=frozenset({"OPENAI_API_KEY"}),
        ),
        manager_environment={"OPENAI_API_KEY": "manager-real-key"},
    )

    result = await runner.run(make_spec(workspace, "show-env"))

    assert "OPENAI_API_KEY" not in json.loads(result.stdout)


@pytest.mark.asyncio
async def test_run_times_out_and_terminates_process(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))

    result = await runner.run(make_spec(workspace, "sleep", timeout_seconds=0.05))

    assert result.status is JobStatus.TIMED_OUT
    assert result.exit_code is not None


@pytest.mark.asyncio
async def test_run_cancels_when_event_is_set(tmp_path: Path, fake_cli: tuple[str, ...]) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))
    cancel_event = asyncio.Event()
    task = asyncio.create_task(runner.run(make_spec(workspace, "sleep"), cancel_event=cancel_event))
    await asyncio.sleep(0.05)

    cancel_event.set()
    result = await asyncio.wait_for(task, timeout=2)

    assert result.status is JobStatus.CANCELLED
    assert result.exit_code is not None


@pytest.mark.asyncio
async def test_run_does_not_start_process_when_already_cancelled(
    tmp_path: Path,
    fake_cli: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))
    cancel_event = asyncio.Event()
    cancel_event.set()
    starts = 0
    original = local_runner_module.asyncio.create_subprocess_exec

    async def counted_start(*args, **kwargs):
        nonlocal starts
        starts += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(local_runner_module.asyncio, "create_subprocess_exec", counted_start)

    result = await runner.run(make_spec(workspace, "show-env"), cancel_event=cancel_event)

    assert result.status is JobStatus.CANCELLED
    assert starts == 0


@pytest.mark.asyncio
async def test_run_classifies_process_resource_exhaustion_as_retryable(
    tmp_path: Path,
    fake_cli: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))

    async def exhausted(*args, **kwargs):
        del args, kwargs
        raise BlockingIOError("process table temporarily full")

    monkeypatch.setattr(local_runner_module.asyncio, "create_subprocess_exec", exhausted)

    with pytest.raises(RetryableAgentError, match="unable to start Agent process"):
        await runner.run(make_spec(workspace, "show-env"))


@pytest.mark.asyncio
async def test_run_fails_and_terminates_when_combined_output_exceeds_limit(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))

    result = await runner.run(make_spec(workspace, "large-output", max_output_bytes=64))

    output_size = len(result.stdout.encode()) + len(result.stderr.encode())
    assert result.status is JobStatus.FAILED
    assert result.output_truncated is True
    assert output_size <= 64
    assert "output limit" in result.stderr.lower()


@pytest.mark.asyncio
async def test_run_terminates_agent_when_workspace_exceeds_limit(
    tmp_path: Path, fake_cli: tuple[str, ...]
) -> None:
    root, workspace = make_workspace(tmp_path)
    current_size = sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file())
    runner = LocalCodexRunner(
        RunnerConfig(
            root,
            fake_cli,
            max_workspace_bytes=current_size + 1024,
        )
    )

    result = await runner.run(make_spec(workspace, "large-workspace"))

    assert result.status is JobStatus.FAILED
    assert result.stderr == "workspace size limit exceeded"
    assert result.output_truncated is False


@pytest.mark.asyncio
async def test_windows_termination_uses_taskkill_for_the_process_tree(
    tmp_path: Path,
    fake_cli: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = make_workspace(tmp_path)
    runner = LocalCodexRunner(RunnerConfig(root, fake_cli, "http://127.0.0.1:9999/v1"))
    killed: list[int] = []

    class Process:
        pid = 42
        returncode = None

        async def wait(self):
            self.returncode = 1
            return 1

    monkeypatch.setattr(local_runner_module.os, "name", "nt")
    monkeypatch.setattr(local_runner_module, "_taskkill", killed.append)

    await runner._terminate(Process())

    assert killed == [42]


@pytest.mark.asyncio
async def test_windows_termination_falls_back_to_killing_parent_when_taskkill_fails(
    tmp_path: Path,
    fake_cli: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = make_workspace(tmp_path)
    runner = LocalCodexRunner(
        RunnerConfig(
            root,
            fake_cli,
            "http://127.0.0.1:9999/v1",
            termination_grace_seconds=0.01,
        )
    )

    class Process:
        pid = 42
        returncode = None
        killed = False
        exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return 1

        def kill(self):
            self.killed = True
            self.returncode = 1
            self.exited.set()

    process = Process()
    monkeypatch.setattr(local_runner_module.os, "name", "nt")
    monkeypatch.setattr(local_runner_module, "_taskkill", lambda pid: None)

    await asyncio.wait_for(runner._terminate(process), timeout=0.1)

    assert process.killed is True
