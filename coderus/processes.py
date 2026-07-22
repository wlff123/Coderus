from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class CommandTimedOut(RuntimeError):
    pass


class CommandOutputLimitExceeded(RuntimeError):
    pass


class CommandResourceLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


async def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    terminate_grace_seconds: float = 1.0,
    max_output_bytes: int = 10 * 1024 * 1024,
    watch_path: Path | None = None,
    max_path_bytes: int | None = None,
) -> ProcessResult:
    _validate_options(timeout_seconds, terminate_grace_seconds, max_output_bytes)
    if (watch_path is None) != (max_path_bytes is None):
        raise ValueError("watch_path and max_path_bytes must be configured together")
    if max_path_bytes is not None and max_path_bytes <= 0:
        raise ValueError("max_path_bytes must be positive")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_async_process_group_options(),
    )
    assert process.stdout is not None
    assert process.stderr is not None
    output = _AsyncOutput(max_output_bytes)
    stdout_reader = asyncio.create_task(output.read(process.stdout, output.stdout))
    stderr_reader = asyncio.create_task(output.read(process.stderr, output.stderr))
    process_waiter = asyncio.create_task(process.wait())
    limit_waiter = asyncio.create_task(output.exceeded.wait())
    resource_exceeded = asyncio.Event()
    resource_monitor = (
        asyncio.create_task(
            _monitor_path_size(Path(watch_path), max_path_bytes, resource_exceeded)
        )
        if watch_path is not None and max_path_bytes is not None
        else None
    )
    resource_waiter = (
        asyncio.create_task(resource_exceeded.wait())
        if resource_monitor is not None
        else None
    )
    try:
        waiters = {process_waiter, limit_waiter}
        if resource_waiter is not None:
            waiters.add(resource_waiter)
        completed, _ = await asyncio.wait(
            waiters,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if limit_waiter in completed and output.exceeded.is_set():
            await _terminate_async_process_group(process, terminate_grace_seconds)
            await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
            raise CommandOutputLimitExceeded("command output limit exceeded")
        if resource_waiter in completed and resource_exceeded.is_set():
            await _terminate_async_process_group(process, terminate_grace_seconds)
            await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
            raise CommandResourceLimitExceeded("command path size limit exceeded")
        if process_waiter not in completed:
            await _terminate_async_process_group(process, terminate_grace_seconds)
            await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
            raise CommandTimedOut("command timed out")
        await asyncio.gather(stdout_reader, stderr_reader)
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(
            _terminate_async_process_group(process, terminate_grace_seconds)
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
        raise
    finally:
        limit_waiter.cancel()
        if resource_waiter is not None:
            resource_waiter.cancel()
        if resource_monitor is not None:
            resource_monitor.cancel()
        await asyncio.gather(
            *(task for task in (limit_waiter, resource_waiter, resource_monitor) if task),
            return_exceptions=True,
        )
    return ProcessResult(process.returncode or 0, bytes(output.stdout), bytes(output.stderr))


def run_process_sync(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    terminate_grace_seconds: float = 1.0,
    max_output_bytes: int = 10 * 1024 * 1024,
) -> ProcessResult:
    _validate_options(timeout_seconds, terminate_grace_seconds, max_output_bytes)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_sync_process_group_options(),
    )
    output = _SyncOutput(max_output_bytes)
    assert process.stdout is not None
    assert process.stderr is not None
    readers = (
        threading.Thread(target=output.read, args=(process.stdout, output.stdout), daemon=True),
        threading.Thread(target=output.read, args=(process.stderr, output.stderr), daemon=True),
    )
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None and not output.exceeded.wait(0.01):
        if time.monotonic() >= deadline:
            _terminate_sync_process_group(process, terminate_grace_seconds)
            for reader in readers:
                reader.join()
            raise CommandTimedOut("command timed out") from None
    if output.exceeded.is_set():
        _terminate_sync_process_group(process, terminate_grace_seconds)
        for reader in readers:
            reader.join()
        raise CommandOutputLimitExceeded("command output limit exceeded")
    for reader in readers:
        reader.join()
    return ProcessResult(process.returncode, bytes(output.stdout), bytes(output.stderr))


def _validate_options(
    timeout_seconds: float, terminate_grace_seconds: float, max_output_bytes: int
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if terminate_grace_seconds <= 0:
        raise ValueError("terminate_grace_seconds must be positive")
    if isinstance(max_output_bytes, bool) or max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")


async def _monitor_path_size(path: Path, limit: int, exceeded: asyncio.Event) -> None:
    while True:
        if await asyncio.to_thread(path_size_exceeds, path, limit):
            exceeded.set()
            return
        await asyncio.sleep(0.1)


def path_size_exceeds(path: Path, limit: int) -> bool:
    if not path.exists():
        return False
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = tuple(os.scandir(current)) if current.is_dir() else ()
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if total > limit:
                return True
    return False


@dataclass
class _AsyncOutput:
    limit: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    total: int = 0
    exceeded: asyncio.Event = field(default_factory=asyncio.Event)

    async def read(self, stream: asyncio.StreamReader, target: bytearray) -> None:
        while chunk := await stream.read(64 * 1024):
            self.total += len(chunk)
            if self.total > self.limit:
                self.exceeded.set()
                return
            target.extend(chunk)


@dataclass
class _SyncOutput:
    limit: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    total: int = 0
    exceeded: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def read(self, stream, target: bytearray) -> None:
        read = getattr(stream, "read1", stream.read)
        while chunk := read(64 * 1024):
            with self.lock:
                self.total += len(chunk)
                if self.total > self.limit:
                    self.exceeded.set()
                    return
                target.extend(chunk)


def _async_process_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _sync_process_group_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _terminate_async_process_group(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    if process.returncode is None:
        if os.name == "nt":
            await asyncio.to_thread(_taskkill, process.pid)
        else:
            _signal_posix_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), grace_seconds)
        return
    except TimeoutError:
        pass
    if process.returncode is None:
        if os.name == "nt":
            await asyncio.to_thread(_taskkill, process.pid)
            process.kill()
        else:
            _signal_posix_group(process.pid, signal.SIGKILL)
    await process.wait()


def _terminate_sync_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> None:
    if process.poll() is None:
        if os.name == "nt":
            _taskkill(process.pid)
        else:
            _signal_posix_group(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        if os.name == "nt":
            _taskkill(process.pid)
            process.kill()
        else:
            _signal_posix_group(process.pid, signal.SIGKILL)
    process.wait()


def _taskkill(pid: int) -> None:
    options: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "check": False,
        "timeout": 5,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.run(("taskkill", "/PID", str(pid), "/T", "/F"), **options)
    except (OSError, subprocess.TimeoutExpired):
        return


def _signal_posix_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
