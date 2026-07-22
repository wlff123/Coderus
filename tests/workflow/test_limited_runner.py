from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from coderus.runner import AgentRole, JobResult, JobSpec, JobStatus, Stage
from coderus.workflow.limited_runner import (
    LimitedRunner,
    RetryableAgentError,
    retry_agent_operation,
)


@dataclass
class BlockingRunner:
    release: asyncio.Event
    calls: int = 0

    async def run(self, spec, *, cancel_event=None):
        self.calls += 1
        await self.release.wait()
        return JobResult(spec.job_id, JobStatus.SUCCEEDED, 0, "", "", False, 0)


def spec(job_id: str, timeout: float = 1) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        stage=Stage.DEVELOP,
        role=AgentRole.DEVELOPER,
        workspace=__import__("pathlib").Path("."),
        prompt="work",
        timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_cancelled_waiter_never_starts_underlying_runner() -> None:
    release = asyncio.Event()
    runner = BlockingRunner(release)
    limited = LimitedRunner(runner, 1)
    first = asyncio.create_task(limited.run(spec("first")))
    await asyncio.sleep(0)
    cancel = asyncio.Event()
    second = asyncio.create_task(limited.run(spec("second"), cancel_event=cancel))
    await asyncio.sleep(0)

    cancel.set()
    result = await second
    release.set()
    await first

    assert result.status is JobStatus.CANCELLED
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_waiting_for_slot_consumes_job_timeout() -> None:
    release = asyncio.Event()
    runner = BlockingRunner(release)
    limited = LimitedRunner(runner, 1)
    first = asyncio.create_task(limited.run(spec("first")))
    await asyncio.sleep(0)

    result = await limited.run(spec("second", timeout=0.01))
    release.set()
    await first

    assert result.status is JobStatus.TIMED_OUT
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_retry_restores_checkpoint_before_retrying_transient_start_error() -> None:
    events: list[str] = []

    async def operation():
        events.append("run")
        if events.count("run") < 3:
            raise RetryableAgentError("temporary startup failure")
        return "done"

    async def restore():
        events.append("restore")

    result = await retry_agent_operation(
        operation,
        restore,
        max_retries=2,
        initial_backoff_seconds=0,
    )

    assert result == "done"
    assert events == ["run", "restore", "run", "restore", "run"]


@pytest.mark.asyncio
async def test_retry_does_not_repeat_unclassified_failure() -> None:
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise RuntimeError("permanent")

    async def restore():
        raise AssertionError("must not restore without a retry")

    with pytest.raises(RuntimeError, match="permanent"):
        await retry_agent_operation(operation, restore, initial_backoff_seconds=0)

    assert calls == 1
