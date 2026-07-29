from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from coderus.processes import _taskkill, path_size_exceeds

from .protocol import JobResult, JobSpec, JobStatus, RetryableAgentError, Stage
from .workspace import validate_workspace

DEFAULT_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TZ",
        "WINDIR",
    }
)

BLOCKED_INHERITED_ENVIRONMENT = frozenset(
    {
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "GITHUB_TOKEN",
        "GITCODE_TOKEN",
        "OPENAI_API_KEY",
        "CODERUS_BOOTSTRAP_ADMIN_PASSWORD",
        "CODERUS_SESSION_SECRET",
    }
)

PROXY_PROVIDER = "coderus_proxy"


def resolve_codex_command(binary: str) -> tuple[str, ...]:
    if not binary:
        raise ValueError("codex binary must not be empty")
    if os.name == "nt":
        candidate = Path(binary)
        if candidate.suffix.lower() == ".exe" and candidate.is_file():
            return (str(candidate.resolve()),)
        resolved = shutil.which(binary)
        if resolved and Path(resolved).suffix.lower() == ".exe":
            return (resolved,)
        if resolved:
            codex_js = (
                Path(resolved).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            )
            node = shutil.which("node.exe")
            if node and codex_js.is_file():
                return (node, str(codex_js))
        native = shutil.which(f"{binary}.exe")
        if native:
            return (native,)
        raise FileNotFoundError("a native codex.exe is required on Windows")
    resolved = shutil.which(binary)
    if resolved is None:
        raise FileNotFoundError(f"Codex executable was not found: {binary}")
    executable = Path(resolved)
    target = executable.resolve(strict=True)
    if target.suffix == ".js":
        sibling_node = executable.parent / "node"
        node = (
            str(sibling_node.resolve(strict=True))
            if sibling_node.is_file()
            else shutil.which("node")
        )
        if node is None:
            raise FileNotFoundError("Node.js is required to run the Codex CLI")
        return (node, str(target))
    return (str(executable),)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    workspace_root: Path
    codex_command: tuple[str, ...] = ("codex",)
    api_base_url: str = "https://api.openai.com/v1"
    model: str | None = None
    network_access: bool = True
    sandbox_mode: str = "workspace-write"
    environment_allowlist: frozenset[str] = field(
        default_factory=lambda: DEFAULT_ENVIRONMENT_ALLOWLIST
    )
    termination_grace_seconds: float = 0.5
    max_workspace_bytes: int = 2 * 1024 * 1024 * 1024
    runtime_root: Path = field(default_factory=lambda: _default_runtime_root())

    def __init__(
        self,
        workspace_root: Path,
        codex_command: Sequence[str] = ("codex",),
        api_base_url: str = "https://api.openai.com/v1",
        model: str | None = None,
        network_access: bool = True,
        environment_allowlist: frozenset[str] = DEFAULT_ENVIRONMENT_ALLOWLIST,
        termination_grace_seconds: float = 0.5,
        sandbox_mode: str = "workspace-write",
        runtime_root: Path | None = None,
        max_workspace_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        if not codex_command:
            raise ValueError("codex_command must not be empty")
        if not api_base_url:
            raise ValueError("api_base_url must not be empty")
        if termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be positive")
        if sandbox_mode not in {"workspace-write", "danger-full-access"}:
            raise ValueError("unsupported Codex sandbox mode")
        if max_workspace_bytes <= 0:
            raise ValueError("max_workspace_bytes must be positive")
        object.__setattr__(self, "workspace_root", Path(workspace_root))
        object.__setattr__(self, "codex_command", tuple(codex_command))
        object.__setattr__(self, "api_base_url", api_base_url)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "network_access", network_access)
        object.__setattr__(self, "sandbox_mode", sandbox_mode)
        object.__setattr__(self, "environment_allowlist", frozenset(environment_allowlist))
        object.__setattr__(self, "termination_grace_seconds", termination_grace_seconds)
        object.__setattr__(self, "max_workspace_bytes", max_workspace_bytes)
        object.__setattr__(
            self,
            "runtime_root",
            Path(runtime_root) if runtime_root is not None else _default_runtime_root(),
        )


def _default_runtime_root() -> Path:
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    return Path(tempfile.gettempdir()) / f"coderus-runtime{suffix}"


def _command_runtime_paths(command: Sequence[str]) -> set[Path]:
    paths: set[Path] = set()
    for argument in command:
        candidate = Path(argument)
        if not candidate.is_absolute() or not candidate.exists():
            continue
        resolved = candidate.resolve(strict=True)
        paths.add(candidate.parent.resolve(strict=True))
        paths.add(resolved)
        for parent in resolved.parents:
            if parent.name == "@openai":
                paths.add(parent)
                break
    return paths


@dataclass(frozen=True, slots=True)
class _RunDirectories:
    root: Path
    home: Path
    codex_home: Path
    temp: Path


@dataclass(slots=True)
class _TailBuffer:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.limit == 0:
            self.truncated = True
            return
        overflow = len(self.data) + len(chunk) - self.limit
        if overflow > 0:
            self.truncated = True
            if overflow >= len(self.data):
                self.data.clear()
                chunk = chunk[-self.limit :]
            else:
                del self.data[:overflow]
        self.data.extend(chunk)


@dataclass(slots=True)
class _OutputBudget:
    limit: int
    total: int = 0
    exceeded: asyncio.Event = field(default_factory=asyncio.Event)

    def consume(self, size: int) -> bool:
        self.total += size
        if self.total > self.limit:
            self.exceeded.set()
            return False
        return True


class LocalCodexRunner:
    def __init__(
        self,
        config: RunnerConfig,
        *,
        manager_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._manager_environment = (
            dict(os.environ) if manager_environment is None else dict(manager_environment)
        )

    def build_command(self, spec: JobSpec, *, output_schema: Path | None = None) -> list[str]:
        return self._build_exec_command(spec, output_schema=output_schema)

    def _proxy_provider_options(self) -> tuple[str, ...]:
        provider = f"model_providers.{PROXY_PROVIDER}"
        return (
            "-c",
            f"model_provider={json.dumps(PROXY_PROVIDER)}",
            "-c",
            f'{provider}.name="Coderus model proxy"',
            "-c",
            f"{provider}.base_url={json.dumps(self._config.api_base_url)}",
            "-c",
            f'{provider}.env_key="OPENAI_API_KEY"',
            "-c",
            f'{provider}.wire_api="responses"',
            "-c",
            f"{provider}.supports_websockets=false",
        )

    def _build_exec_command(
        self,
        spec: JobSpec,
        *,
        output_schema: Path | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        if spec.stage is Stage.PR_REVIEW:
            # The service container cannot create Codex's bwrap namespace. On Linux the
            # enclosing Landlock launcher blocks workspace content writes.
            sandbox = "danger-full-access" if sys.platform == "linux" else "read-only"
        elif self._config.sandbox_mode == "danger-full-access":
            sandbox = "danger-full-access"
        elif spec.role.read_only:
            sandbox = "read-only"
        else:
            sandbox = "workspace-write"
        command = [
            *self._config.codex_command,
            "exec",
            "--json",
        ]
        command.extend(("--sandbox", sandbox, "-c", 'approval_policy="never"'))
        command.extend(("--ephemeral", "--ignore-user-config", "--ignore-rules"))
        if spec.stage is Stage.PR_REVIEW:
            command.extend(("-c", "project_doc_max_bytes=0"))
            command.extend(("-c", f"developer_instructions={json.dumps(spec.prompt)}"))
        if os.name == "nt":
            command.extend(("-c", 'windows.sandbox="unelevated"'))
        if self._config.network_access:
            command.extend(("-c", "sandbox_workspace_write.network_access=true"))
        if spec.proxy_token is not None:
            command.extend(self._proxy_provider_options())
        if self._config.model:
            command.extend(("--model", self._config.model))
        if output_schema is not None:
            command.extend(("--output-schema", str(output_schema)))
        if spec.session_id is not None:
            command.extend(("resume", spec.session_id))
        if spec.stage is Stage.PR_REVIEW:
            command.extend(("review", "--base", spec.review_base))
        else:
            command.append(prompt if prompt is not None else spec.prompt)
        return command

    def build_environment(
        self,
        spec: JobSpec,
        *,
        run_directories: _RunDirectories,
    ) -> dict[str, str]:
        allowed = {name.upper() for name in self._config.environment_allowlist}
        environment = {
            name: value
            for name, value in self._manager_environment.items()
            if name.upper() in allowed and name.upper() not in BLOCKED_INHERITED_ENVIRONMENT
        }
        environment.update(
            HOME=str(run_directories.home),
            USERPROFILE=str(run_directories.home),
            CODEX_HOME=str(run_directories.codex_home),
            XDG_CONFIG_HOME=str(run_directories.home / ".config"),
            XDG_CACHE_HOME=str(run_directories.home / ".cache"),
            TEMP=str(run_directories.temp),
            TMP=str(run_directories.temp),
            TMPDIR=str(run_directories.temp),
            GIT_CONFIG_GLOBAL=str(run_directories.home / ".gitconfig"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )
        environment["OPENAI_BASE_URL"] = self._config.api_base_url
        environment["PIP_REQUIRE_VIRTUALENV"] = "true"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        if spec.proxy_token is not None:
            environment["OPENAI_API_KEY"] = spec.proxy_token
        return environment

    async def run(self, spec: JobSpec, *, cancel_event: asyncio.Event | None = None) -> JobResult:
        started = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            return JobResult(
                job_id=spec.job_id,
                status=JobStatus.CANCELLED,
                exit_code=None,
                stdout="",
                stderr="cancelled before Agent process start",
                output_truncated=False,
                duration_seconds=time.monotonic() - started,
            )
        workspace = validate_workspace(self._config.workspace_root, spec.workspace)
        run_directories = self._create_run_directories(workspace)
        try:
            output_schema = self._copy_output_schema(spec, run_directories)
            command = self.build_command(spec, output_schema=output_schema)
            environment = self.build_environment(spec, run_directories=run_directories)
            if os.name == "posix":
                command = self._landlock_command(
                    spec,
                    workspace,
                    run_directories,
                    command,
                )
            result = await self._run_process(
                spec,
                command,
                workspace,
                environment,
                started,
                cancel_event,
                stdin_text=None,
            )
            return result
        finally:
            self._remove_run_directories(run_directories)

    async def _run_process(
        self,
        spec: JobSpec,
        command: list[str],
        workspace: Path,
        environment: Mapping[str, str],
        started: float,
        cancel_event: asyncio.Event | None,
        *,
        stdin_text: str | None = None,
    ) -> JobResult:
        process_options: dict[str, object] = {}
        if os.name == "posix":
            process_options["start_new_session"] = True
        elif os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workspace,
                env=dict(environment),
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin_text is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
        except OSError as exc:
            retryable_errno = {errno.EAGAIN, errno.EMFILE, errno.ENFILE, errno.ENOMEM}
            retryable_winerror = {8, 1450}
            if (
                isinstance(exc, BlockingIOError)
                or exc.errno in retryable_errno
                or getattr(exc, "winerror", None) in retryable_winerror
            ):
                raise RetryableAgentError("unable to start Agent process") from exc
            raise
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_limit = max(
            1,
            spec.max_output_bytes // 2
            if spec.stage is Stage.PR_REVIEW
            else spec.max_output_bytes * 4 // 5,
        )
        stderr_limit = spec.max_output_bytes - stdout_limit
        output_budget = _OutputBudget(spec.max_output_bytes)
        stdout_task = asyncio.create_task(
            self._read_output(process.stdout, stdout_limit, output_budget)
        )
        stderr_task = asyncio.create_task(
            self._read_output(process.stderr, stderr_limit, output_budget)
        )
        input_task = None
        if stdin_text is not None:
            assert process.stdin is not None
            input_task = asyncio.create_task(self._write_input(process.stdin, stdin_text))
        process_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        output_limit_task = asyncio.create_task(output_budget.exceeded.wait())
        workspace_limit = asyncio.Event()
        workspace_monitor = asyncio.create_task(self._monitor_workspace(workspace, workspace_limit))
        workspace_limit_task = asyncio.create_task(workspace_limit.wait())

        try:
            wait_for = {process_task, output_limit_task, workspace_limit_task}
            if cancel_task is not None:
                wait_for.add(cancel_task)
            completed, _ = await asyncio.wait(
                wait_for, timeout=spec.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )

            if output_limit_task in completed and output_budget.exceeded.is_set():
                status = JobStatus.FAILED
                await self._terminate(process)
            elif workspace_limit_task in completed and workspace_limit.is_set():
                status = JobStatus.FAILED
                await self._terminate(process)
            elif process_task in completed:
                status = JobStatus.SUCCEEDED if process.returncode == 0 else JobStatus.FAILED
            elif cancel_task is not None and cancel_task in completed:
                status = JobStatus.CANCELLED
                await self._terminate(process)
            else:
                status = JobStatus.TIMED_OUT
                await self._terminate(process)

            await process_task
            stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
            if output_budget.exceeded.is_set():
                stdout_result = (b"", True)
                stderr_result = (b"output limit exceeded", True)
            elif workspace_limit.is_set():
                stdout_result = (b"", False)
                stderr_result = (b"workspace size limit exceeded", False)
            if input_task is not None:
                await input_task
        except asyncio.CancelledError:
            await self._terminate(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            if input_task is not None:
                input_task.cancel()
                await asyncio.gather(input_task, return_exceptions=True)
            raise
        finally:
            output_limit_task.cancel()
            workspace_limit_task.cancel()
            workspace_monitor.cancel()
            await asyncio.gather(
                output_limit_task,
                workspace_limit_task,
                workspace_monitor,
                return_exceptions=True,
            )
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

        return JobResult(
            job_id=spec.job_id,
            status=status,
            exit_code=process.returncode,
            stdout=stdout_result[0].decode("utf-8", errors="ignore"),
            stderr=stderr_result[0].decode("utf-8", errors="ignore"),
            output_truncated=stdout_result[1] or stderr_result[1],
            duration_seconds=time.monotonic() - started,
        )

    async def _monitor_workspace(self, workspace: Path, exceeded: asyncio.Event) -> None:
        while True:
            if await asyncio.to_thread(
                path_size_exceeds, workspace, self._config.max_workspace_bytes
            ):
                exceeded.set()
                return
            await asyncio.sleep(0.1)

    def _create_run_directories(self, workspace: Path) -> _RunDirectories:
        runtime_root = self._validated_runtime_root(workspace)
        root = Path(tempfile.mkdtemp(prefix="run-", dir=runtime_root)).resolve(strict=True)
        if root.parent != runtime_root:
            raise ValueError("runtime directory escapes runtime root")
        root.chmod(0o700)
        home = root / "home"
        codex_home = root / "codex-home"
        temp = root / "tmp"
        for directory in (home, codex_home, temp):
            directory.mkdir(mode=0o700)
        (codex_home / "tmp" / "arg0").mkdir(parents=True, mode=0o700)
        git_config = home / ".gitconfig"
        git_config.write_text("", encoding="utf-8")
        git_config.chmod(0o600)
        return _RunDirectories(root=root, home=home, codex_home=codex_home, temp=temp)

    def _validated_runtime_root(self, workspace: Path) -> Path:
        configured = Path(os.path.abspath(self._config.runtime_root))
        self._reject_symlink_components(configured)
        configured.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._reject_symlink_components(configured)
        resolved = configured.resolve(strict=True)
        workspace_root = self._config.workspace_root.resolve(strict=True)
        if resolved == workspace_root or resolved.is_relative_to(workspace_root):
            raise ValueError("runtime root must be outside the workspace root")
        if not resolved.is_dir():
            raise ValueError("runtime root must be a directory")
        if hasattr(os, "getuid") and resolved.stat().st_uid != os.getuid():
            raise ValueError("runtime root must be owned by the manager user")
        resolved.chmod(0o700)
        if workspace.resolve(strict=True) == resolved:
            raise ValueError("runtime root must be outside the workspace")
        return resolved

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        components = [path]
        components.extend(path.parents)
        for component in reversed(components):
            try:
                mode = component.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode) or (
                hasattr(component, "is_junction") and component.is_junction()
            ):
                raise ValueError("runtime root must not contain symlink components")

    @staticmethod
    def _copy_output_schema(spec: JobSpec, directories: _RunDirectories) -> Path | None:
        if spec.output_schema is None:
            return None
        source = Path(spec.output_schema)
        try:
            mode = source.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError("output schema must be an existing regular file") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("output schema must be an existing regular file")
        destination = directories.root / "output-schema.json"
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        return destination

    def _landlock_command(
        self,
        spec: JobSpec,
        workspace: Path,
        directories: _RunDirectories,
        command: list[str],
    ) -> list[str]:
        runtime_paths = {
            str(Path(sys.prefix).resolve()),
            str(Path(sys.base_prefix).resolve()),
            str(Path(sys.executable).resolve()),
        }
        runtime_paths.update(
            str(path) for path in _command_runtime_paths(self._config.codex_command)
        )
        runtime_paths.add(str(directories.codex_home / "tmp" / "arg0"))
        policy = {
            "workspace": str(workspace),
            "workspace_writable": not spec.role.read_only,
            "workspace_executable": spec.stage is not Stage.PR_REVIEW,
            "run_root": str(directories.root),
            "runtime_paths": sorted(runtime_paths),
        }
        policy_path = directories.root / "landlock-policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        policy_path.chmod(0o600)
        launcher = Path(__file__).with_name("landlock.py")
        return [
            sys.executable,
            "-I",
            str(launcher),
            str(policy_path),
            "--",
            *command,
        ]

    def _remove_run_directories(self, directories: _RunDirectories) -> None:
        runtime_root = Path(os.path.abspath(self._config.runtime_root)).resolve(strict=True)
        root = directories.root.resolve(strict=False)
        if root.parent != runtime_root or root.name in {"", ".", ".."}:
            raise RuntimeError("refusing to remove path outside runtime root")
        shutil.rmtree(root, ignore_errors=False)

    @staticmethod
    async def _read_output(
        stream: asyncio.StreamReader, limit: int, budget: _OutputBudget
    ) -> tuple[bytes, bool]:
        output = _TailBuffer(limit)
        while data := await stream.read(64 * 1024):
            if not budget.consume(len(data)):
                return bytes(output.data), True
            output.append(data)
        return bytes(output.data), output.truncated

    @staticmethod
    async def _write_input(stream: asyncio.StreamWriter, value: str) -> None:
        try:
            stream.write(value.encode("utf-8"))
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stream.close()
            try:
                await stream.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            await asyncio.to_thread(_taskkill, process.pid)
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self._config.termination_grace_seconds
                )
                return
            except TimeoutError:
                process.kill()
            await process.wait()
            return
        self._send_termination(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self._config.termination_grace_seconds)
        except TimeoutError:
            self._send_termination(process, signal.SIGKILL)
            await process.wait()

    @staticmethod
    def _send_termination(
        process: asyncio.subprocess.Process, requested_signal: signal.Signals
    ) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, requested_signal)
            elif requested_signal is signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass
