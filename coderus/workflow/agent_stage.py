"""单个 Agent 阶段的执行器：AgentRun 记账、短时凭据和结果解析。

状态 CAS 仍归 Orchestrator，通过构造时注入的 transition 回调完成。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.model_proxy import UsageSnapshot, issued_stage_token
from coderus.models import AgentRun
from coderus.runner import AgentRole, JobSpec, JobStatus, Stage
from coderus.workflow.developer_report import (
    DeveloperReport,
    developer_report_schema_path,
    parse_developer_report,
)
from coderus.workflow.limited_runner import retry_agent_operation
from coderus.workflow.reviewer_result import reviewer_result_schema_path


class TaskCancelled(Exception):
    pass


async def _noop_checkpoint_restore() -> None:
    # RetryableAgentError is only raised before the child process starts.
    return None


def final_message(stdout: str) -> str:
    message = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {}) if isinstance(event, dict) else {}
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "agent_message"
        ):
            message = str(item.get("text", ""))
    return message or stdout[-8000:]


class AgentStageExecutor:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        runner: object,
        credential_broker: object | None,
        stage_timeout_seconds: int,
        transition: Callable[[int, str, str], None],
    ) -> None:
        self.sessions = session_factory
        self.runner = runner
        self.credential_broker = credential_broker
        self.stage_timeout_seconds = stage_timeout_seconds
        self.transition = transition

    async def execute(
        self,
        *,
        task_id: int,
        status: str,
        stage: Stage,
        role: AgentRole,
        workspace: Path,
        prompt: str,
        claim_token: str,
        cancel_event: asyncio.Event,
    ):
        self.transition(task_id, status, claim_token)
        run_id, attempt = self._begin_agent_run(task_id, role)
        model_usage: UsageSnapshot | None = None
        try:
            with issued_stage_token(
                self.credential_broker,
                task_id=f"task-{task_id}",
                stage=stage.value,
                ttl_seconds=self.stage_timeout_seconds + 300,
            ) as proxy_token:
                spec = JobSpec(
                    job_id=f"task-{task_id}-{role.value}-{attempt}",
                    stage=stage,
                    role=role,
                    workspace=workspace,
                    prompt=prompt,
                    timeout_seconds=self.stage_timeout_seconds,
                    proxy_token=proxy_token,
                    output_schema=(
                        developer_report_schema_path()
                        if role == AgentRole.DEVELOPER
                        else reviewer_result_schema_path()
                    ),
                )
                try:
                    result = await retry_agent_operation(
                        lambda: self.runner.run(spec, cancel_event=cancel_event),
                        _noop_checkpoint_restore,
                    )
                finally:
                    # 用量随最后一个令牌撤销一并清除，必须在离开凭据上下文前读取。
                    model_usage = self._stage_usage(task_id, stage)
            developer_report = None
            if result.status is JobStatus.SUCCEEDED and role == AgentRole.DEVELOPER:
                developer_report = parse_developer_report(final_message(result.stdout))
        except BaseException as exc:
            self._finish_agent_run(
                run_id,
                status=(
                    "interrupted"
                    if isinstance(exc, asyncio.CancelledError)
                    else "failed"
                ),
                error_summary=str(exc)[-2000:] or type(exc).__name__,
                model_usage=model_usage,
            )
            raise
        self._finish_agent_run(
            run_id,
            status=result.status.value,
            exit_code=result.exit_code,
            stdout=result.stdout,
            error_summary=result.stderr[-2000:] or None,
            developer_report=developer_report,
            model_usage=model_usage,
        )
        if result.status is JobStatus.CANCELLED:
            raise TaskCancelled
        if result.status is not JobStatus.SUCCEEDED:
            raise RuntimeError(f"{role.value} failed: {result.stderr[-500:]}")
        return result

    def _begin_agent_run(self, task_id: int, role: AgentRole) -> tuple[int, int]:
        with self.sessions() as session:
            attempt = (
                session.scalar(
                    select(AgentRun.attempt)
                    .where(AgentRun.task_id == task_id, AgentRun.role == role.value)
                    .order_by(AgentRun.attempt.desc())
                    .limit(1)
                )
                or 0
            ) + 1
            run = AgentRun(
                task_id=task_id,
                role=role.value,
                attempt=attempt,
                status="running",
                started_at=datetime.now(UTC),
            )
            session.add(run)
            session.commit()
            return run.id, attempt

    def _stage_usage(self, task_id: int, stage: Stage) -> UsageSnapshot | None:
        if self.credential_broker is None:
            return None
        return self.credential_broker.usage(f"task-{task_id}", stage.value)

    def _finish_agent_run(
        self,
        run_id: int,
        *,
        status: str,
        exit_code: int | None = None,
        stdout: str | None = None,
        error_summary: str | None = None,
        developer_report: DeveloperReport | None = None,
        model_usage: UsageSnapshot | None = None,
    ) -> None:
        with self.sessions() as session:
            run = session.get(AgentRun, run_id)
            if run is None or run.status != "running":
                return
            run.status = status
            run.finished_at = datetime.now(UTC)
            run.exit_code = exit_code
            structured: dict = {}
            if stdout is not None:
                structured["stdout"] = stdout[-100_000:]
                if developer_report is not None:
                    structured["developer_report"] = developer_report.model_dump()
            if model_usage is not None:
                structured["model_usage"] = {
                    "request_count": model_usage.request_count,
                    "output_bytes": model_usage.output_bytes,
                }
            run.structured_result = structured or None
            run.error_summary = error_summary
            session.commit()
