"""AgentStageExecutor：状态回调、AgentRun 记账、短时凭据与结果契约。"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.models import AgentRun, Issue, Repository, Task, User
from coderus.runner import AgentRole, JobResult, JobStatus, Stage
from coderus.workflow.agent_stage import AgentStageExecutor, TaskCancelled

DEVELOPER_REPORT = {
    "problem_description": "启动崩溃",
    "problem_reproduction": "已复现",
    "solution": "修复空指针",
    "change_validation": "diff 已检查",
    "regression_tests": "测试已运行",
    "remaining_issues": "无已知遗留问题",
}


def agent_stdout(payload: dict) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(payload, ensure_ascii=False),
            },
        }
    )


class FakeRunner:
    def __init__(self, status: JobStatus, stdout: str = "", stderr: str = "") -> None:
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
        self.specs = []

    async def run(self, spec, *, cancel_event: asyncio.Event) -> JobResult:
        self.specs.append(spec)
        return JobResult(
            job_id=spec.job_id,
            status=self.status,
            exit_code=0 if self.status is JobStatus.SUCCEEDED else 1,
            stdout=self.stdout,
            stderr=self.stderr,
            output_truncated=False,
            duration_seconds=0.1,
        )


class FakeBroker:
    def __init__(self) -> None:
        self.issued: list[dict] = []
        self.revoked: list[str] = []

    def issue(self, *, task_id: str, stage: str, ttl_seconds: int) -> str:
        self.issued.append(
            {"task_id": task_id, "stage": stage, "ttl_seconds": ttl_seconds}
        )
        return f"token-{len(self.issued)}"

    def revoke(self, token: str) -> None:
        self.revoked.append(token)


def seed_task(engine) -> int:
    with Session(engine) as session:
        user = User(username="dev", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            created_by_user=user,
        )
        issue = Issue(
            repository=repository,
            external_id="1",
            number=1,
            title="crash",
            body="detail",
            state="open",
            source_url="https://github.com/octo/demo/issues/1",
        )
        issue.triage_state = "dispatched"
        task = Task(issue=issue, creator=user, status="preparing")
        session.add(task)
        session.commit()
        return task.id


def executor(engine, runner, broker=None, transitions=None):
    recorded = transitions if transitions is not None else []
    return AgentStageExecutor(
        session_factory=lambda: Session(engine),
        runner=runner,
        credential_broker=broker,
        stage_timeout_seconds=600,
        transition=lambda task_id, status, claim: recorded.append(
            (task_id, status, claim)
        ),
    )


async def run_develop(stage_executor, task_id: int, tmp_path):
    return await stage_executor.execute(
        task_id=task_id,
        status="developer_working",
        stage=Stage.DEVELOP,
        role=AgentRole.DEVELOPER,
        workspace=tmp_path,
        prompt="work",
        claim_token="claim",
        cancel_event=asyncio.Event(),
    )


def test_success_saves_report_and_revokes_token(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    runner = FakeRunner(JobStatus.SUCCEEDED, stdout=agent_stdout(DEVELOPER_REPORT))
    broker = FakeBroker()
    transitions: list = []
    stage_executor = executor(engine, runner, broker, transitions)

    result = asyncio.run(run_develop(stage_executor, task_id, tmp_path))

    assert result.status is JobStatus.SUCCEEDED
    assert transitions == [(task_id, "developer_working", "claim")]
    assert broker.issued == [
        {"task_id": f"task-{task_id}", "stage": "develop", "ttl_seconds": 900}
    ]
    assert broker.revoked == ["token-1"]
    assert runner.specs[0].proxy_token == "token-1"
    with Session(engine) as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        assert run.status == "succeeded"
        assert run.attempt == 1
        assert run.finished_at is not None
        report = run.structured_result["developer_report"]
        assert report["problem_description"] == "启动崩溃"


def test_attempt_increments_per_role(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    runner = FakeRunner(JobStatus.SUCCEEDED, stdout=agent_stdout(DEVELOPER_REPORT))
    stage_executor = executor(engine, runner)

    asyncio.run(run_develop(stage_executor, task_id, tmp_path))
    asyncio.run(run_develop(stage_executor, task_id, tmp_path))

    with Session(engine) as session:
        attempts = session.scalars(
            select(AgentRun.attempt)
            .where(AgentRun.task_id == task_id)
            .order_by(AgentRun.id)
        ).all()
        assert attempts == [1, 2]
    assert runner.specs[1].job_id == f"task-{task_id}-developer-2"


def test_failed_run_raises_and_persists_stderr(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    runner = FakeRunner(JobStatus.FAILED, stderr="boom")
    broker = FakeBroker()
    stage_executor = executor(engine, runner, broker)

    with pytest.raises(RuntimeError, match="developer failed"):
        asyncio.run(run_develop(stage_executor, task_id, tmp_path))

    assert broker.revoked == ["token-1"]
    with Session(engine) as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        assert run.status == "failed"
        assert run.error_summary == "boom"


def test_cancelled_run_raises_task_cancelled(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    runner = FakeRunner(JobStatus.CANCELLED)
    stage_executor = executor(engine, runner)

    with pytest.raises(TaskCancelled):
        asyncio.run(run_develop(stage_executor, task_id, tmp_path))

    with Session(engine) as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        assert run.status == "cancelled"


def test_runner_exception_marks_run_failed_and_revokes(engine, tmp_path) -> None:
    task_id = seed_task(engine)

    class ExplodingRunner:
        async def run(self, spec, *, cancel_event):
            raise ValueError("runner exploded")

    broker = FakeBroker()
    stage_executor = executor(engine, ExplodingRunner(), broker)

    with pytest.raises(ValueError, match="runner exploded"):
        asyncio.run(run_develop(stage_executor, task_id, tmp_path))

    assert broker.revoked == ["token-1"]
    with Session(engine) as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        assert run.status == "failed"
        assert "runner exploded" in run.error_summary
