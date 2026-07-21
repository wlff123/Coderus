from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class CommandTimedOut(RuntimeError):
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
) -> ProcessResult:
    _validate_timeouts(timeout_seconds, terminate_grace_seconds)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_async_process_group_options(),
    )
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communication), timeout_seconds
        )
    except TimeoutError:
        await _terminate_async_process_group(
            process, communication, terminate_grace_seconds
        )
        raise CommandTimedOut("command timed out") from None
    except asyncio.CancelledError:
        cleanup = asyncio.create_task(
            _terminate_async_process_group(
                process, communication, terminate_grace_seconds
            )
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        raise
    return ProcessResult(process.returncode or 0, stdout, stderr)


def run_process_sync(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    terminate_grace_seconds: float = 1.0,
) -> ProcessResult:
    _validate_timeouts(timeout_seconds, terminate_grace_seconds)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_sync_process_group_options(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_sync_process_group(process, terminate_grace_seconds)
        raise CommandTimedOut("command timed out") from None
    return ProcessResult(process.returncode, stdout, stderr)


def _validate_timeouts(timeout_seconds: float, terminate_grace_seconds: float) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if terminate_grace_seconds <= 0:
        raise ValueError("terminate_grace_seconds must be positive")


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
    communication: asyncio.Task[tuple[bytes, bytes]],
    grace_seconds: float,
) -> None:
    if process.returncode is None:
        if os.name == "nt":
            await asyncio.to_thread(_taskkill, process.pid)
        else:
            _signal_posix_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(communication), grace_seconds)
        return
    except TimeoutError:
        pass
    if process.returncode is None:
        if os.name == "nt":
            await asyncio.to_thread(_taskkill, process.pid)
            process.kill()
        else:
            _signal_posix_group(process.pid, signal.SIGKILL)
    await communication


def _terminate_sync_process_group(
    process: subprocess.Popen[bytes], grace_seconds: float
) -> None:
    if process.poll() is None:
        if os.name == "nt":
            _taskkill(process.pid)
        else:
            _signal_posix_group(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        if os.name == "nt":
            _taskkill(process.pid)
            process.kill()
        else:
            _signal_posix_group(process.pid, signal.SIGKILL)
    process.communicate()


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
