from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from coderus.runner import JobResult, JobStatus, RetryableAgentError


async def retry_agent_operation[T](
    operation: Callable[[], Awaitable[T]],
    restore_checkpoint: Callable[[], Awaitable[None]],
    *,
    max_retries: int = 2,
    initial_backoff_seconds: float = 0.25,
) -> T:
    if not 0 <= max_retries <= 2:
        raise ValueError("max_retries must be between 0 and 2")
    if initial_backoff_seconds < 0:
        raise ValueError("initial_backoff_seconds must not be negative")
    for retry in range(max_retries + 1):
        try:
            return await operation()
        except RetryableAgentError:
            if retry == max_retries:
                raise
            await restore_checkpoint()
            await asyncio.sleep(initial_backoff_seconds * (2**retry))
    raise RuntimeError("unreachable")


class LimitedRunner:
    def __init__(self, runner: object, limit: int) -> None:
        if limit < 1:
            raise ValueError("runner limit must be positive")
        self._runner = runner
        self._semaphore = asyncio.Semaphore(limit)

    async def run(self, spec, *, cancel_event: asyncio.Event | None = None):
        started = time.monotonic()
        deadline = started + spec.timeout_seconds
        if cancel_event is not None and cancel_event.is_set():
            return self._waiting_result(spec, JobStatus.CANCELLED, started)

        acquire = asyncio.create_task(self._semaphore.acquire())
        cancel = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        acquired = False
        try:
            waiters = {acquire}
            if cancel is not None:
                waiters.add(cancel)
            completed, _ = await asyncio.wait(
                waiters,
                timeout=max(0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel is not None and cancel in completed:
                return self._waiting_result(spec, JobStatus.CANCELLED, started)
            if acquire not in completed:
                return self._waiting_result(spec, JobStatus.TIMED_OUT, started)
            acquired = True
            if cancel_event is not None and cancel_event.is_set():
                return self._waiting_result(spec, JobStatus.CANCELLED, started)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._waiting_result(spec, JobStatus.TIMED_OUT, started)
            return await self._runner.run(
                replace(spec, timeout_seconds=remaining),
                cancel_event=cancel_event,
            )
        finally:
            if not acquire.done():
                acquire.cancel()
            await asyncio.gather(acquire, return_exceptions=True)
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                acquired = acquired or bool(acquire.result())
            if acquired:
                self._semaphore.release()
            if cancel is not None:
                cancel.cancel()
                await asyncio.gather(cancel, return_exceptions=True)

    @staticmethod
    def _waiting_result(spec, status: JobStatus, started: float) -> JobResult:
        return JobResult(
            job_id=spec.job_id,
            status=status,
            exit_code=None,
            stdout="",
            stderr=(
                "cancelled while waiting for an Agent slot"
                if status is JobStatus.CANCELLED
                else "timed out while waiting for an Agent slot"
            ),
            output_truncated=False,
            duration_seconds=time.monotonic() - started,
        )
