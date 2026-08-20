from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from sqlalchemy import case, update
from sqlalchemy.orm import Session

from coderus.forge import ForgeRegistry
from coderus.models import PRReviewTask
from coderus.pr_review.instructions import build_review_instructions
from coderus.pr_review.models import ReviewInput, ReviewOutput
from coderus.pr_review.result import (
    ReviewOutputError,
    parse_review_output,
    render_pr_comment,
    validate_findings,
)
from coderus.providers import ProviderName
from coderus.runner import AgentRole, JobSpec, JobStatus, Stage
from coderus.workflow.limited_runner import retry_agent_operation

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ("preparing", "reviewing", "commenting")
CLAIM_LEASE_SECONDS = 120.0
CLAIM_HEARTBEAT_SECONDS = 10.0
COMMENT_TIMEOUT_SECONDS = 90.0
PR_REVIEW_MAX_OUTPUT_BYTES = 5_000_000
LGTM_COMMENT = "/lgtm"


class Runner(Protocol):
    async def run(self, spec: JobSpec, *, cancel_event=None): ...


class PRReviewError(RuntimeError):
    """A controlled error safe to persist and send to the requester."""


class _ClaimLost(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _TaskContext:
    id: int
    provider: ProviderName
    owner: str
    name: str
    repository_url: str
    pr_number: int
    source_chat_id: str
    review_key: str


class PRReviewOrchestrator:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        forges: ForgeRegistry,
        runner: Runner,
        workspace: object,
        notifier: object | None = None,
        credential_broker: object | None = None,
        stage_timeout_seconds: float = 3600,
    ) -> None:
        self.sessions = session_factory
        self.forges = forges
        self.runner = runner
        self.workspace = workspace
        self.notifier = notifier
        self.credential_broker = credential_broker
        self.stage_timeout_seconds = stage_timeout_seconds

    async def run(self, task_id: int) -> None:
        claim_token = self._claim(task_id)
        if claim_token is None:
            return
        heartbeat_stop = asyncio.Event()
        claim_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(task_id, claim_token, heartbeat_stop, claim_lost),
            name=f"pr-review-heartbeat-{task_id}",
        )
        try:
            task: _TaskContext | None = None
            state = "preparing"
            try:
                task = self._load_task(task_id)
                self._assert_claim(task_id, claim_token, claim_lost)
                details = await self.forges.require(task.provider).get_pull_request(
                    task.owner, task.name, task.pr_number
                )
                self._record_revision(
                    task_id,
                    state,
                    claim_token,
                    details.base_sha,
                    details.head_sha,
                )
                self._validate_pr(details)

                self._assert_claim(task_id, claim_token, claim_lost)
                prepared = await self.workspace.prepare(
                    task_id=task.id,
                    repository_url=task.repository_url,
                    pr_number=task.pr_number,
                    base_ref=details.base_ref,
                    base_sha=details.base_sha,
                    head_sha=details.head_sha,
                    head_ref=details.head_ref,
                    head_repository_url=details.head_repository_url,
                )
                self._transition(task_id, state, "reviewing", claim_token)
                state = "reviewing"
                self._assert_claim(task_id, claim_token, claim_lost)
                material = await self.workspace.review_input(
                    prepared, details.base_sha, details.head_sha
                )
                ranges = material.ranges
                if ranges.comparison_sha is None:
                    raise PRReviewError("无法确认 PR 的实际比较基准")
                self._assert_claim(task_id, claim_token, claim_lost)
                generated_output, proxy_token = await self._run_review(
                    task,
                    prepared,
                    ranges.comparison_sha,
                    details.head_sha,
                    material,
                    claim_lost,
                )
                self._assert_claim(task_id, claim_token, claim_lost)
                await self.workspace.assert_pristine(
                    prepared,
                    details.head_sha,
                    ranges.comparison_sha,
                )
                self._assert_claim(task_id, claim_token, claim_lost)
                output = validate_findings(generated_output, ranges)
                filtered_finding_count = len(generated_output.findings) - len(output.findings)

                self._assert_claim(task_id, claim_token, claim_lost)
                current = await self.forges.require(task.provider).get_pull_request(
                    task.owner, task.name, task.pr_number
                )
                self._validate_pr(
                    current,
                    expected_base_sha=details.base_sha,
                    expected_head_sha=details.head_sha,
                    expected_base_ref=details.base_ref,
                    expected_head_ref=details.head_ref,
                    expected_head_repository_url=details.head_repository_url,
                )
                output = self._redact_output(output, proxy_token, prepared)
                structured_result = output.model_dump(mode="json")
                structured_result["review_audit"] = {
                    "comparison_sha": ranges.comparison_sha,
                    "changed_files": ranges.changed_file_count,
                    "additions": ranges.additions,
                    "deletions": ranges.deletions,
                    "review_mode": "native",
                    "generated_findings": len(generated_output.findings),
                    "validated_findings": len(output.findings),
                    "filtered_findings": filtered_finding_count,
                }
                self._set_commenting(
                    task_id,
                    state,
                    claim_token,
                    structured_result,
                )
                state = "commenting"
                body, marker = render_pr_comment(
                    task.provider,
                    output,
                    task.owner,
                    task.name,
                    details.base_sha,
                    details.head_sha,
                    task.review_key,
                    changed_file_count=ranges.changed_file_count,
                    additions=ranges.additions,
                    deletions=ranges.deletions,
                    filtered_finding_count=filtered_finding_count,
                    comparison_sha=ranges.comparison_sha,
                )
                self._assert_claim(task_id, claim_token, claim_lost)
                comment = await asyncio.wait_for(
                    self.forges.require(task.provider).publish_pr_comment(
                        task.owner, task.name, task.pr_number, body, marker
                    ),
                    timeout=COMMENT_TIMEOUT_SECONDS,
                )
                if not generated_output.findings:
                    self._assert_claim(task_id, claim_token, claim_lost)
                    await asyncio.wait_for(
                        self.forges.require(task.provider).publish_pr_comment(
                            task.owner,
                            task.name,
                            task.pr_number,
                            LGTM_COMMENT,
                            LGTM_COMMENT,
                        ),
                        timeout=COMMENT_TIMEOUT_SECONDS,
                    )
                self._complete(task_id, state, claim_token, comment.url)
            except _ClaimLost:
                return
            except Exception as exc:
                summary = self._safe_summary(exc)
                if not self._fail(task_id, state, claim_token, exc, summary):
                    return
                if task is not None:
                    await self._notify(
                        task.id,
                        task.source_chat_id,
                        f"RV-{task.id} 检视失败：{summary}",
                        expected_status="failed",
                    )
                return

            await self._notify(
                task.id,
                task.source_chat_id,
                f"RV-{task.id} 检视完成，已发布 PR 评论：{comment.url}",
                expected_status="completed",
            )
        finally:
            heartbeat_stop.set()
            await heartbeat

    async def _heartbeat(
        self,
        task_id: int,
        claim_token: str,
        stop: asyncio.Event,
        claim_lost: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=CLAIM_HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                try:
                    renewed = self._renew_claim(task_id, claim_token)
                except Exception as exc:
                    logger.error(
                        "PR review heartbeat failed for %s: %s",
                        task_id,
                        type(exc).__name__,
                    )
                    claim_lost.set()
                    return
                if not renewed:
                    claim_lost.set()
                    return

    def _renew_claim(self, task_id: int, claim_token: str) -> bool:
        now = datetime.now(UTC)
        with self.sessions() as session:
            result = session.execute(
                update(PRReviewTask)
                .where(
                    PRReviewTask.id == task_id,
                    PRReviewTask.status.in_(ACTIVE_STATUSES),
                    PRReviewTask.claim_token == claim_token,
                    PRReviewTask.claim_expires_at > now,
                )
                .values(claim_expires_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS))
            )
            session.commit()
            return result.rowcount == 1

    def _assert_claim(self, task_id: int, claim_token: str, claim_lost: asyncio.Event) -> None:
        if claim_lost.is_set() or not self._renew_claim(task_id, claim_token):
            claim_lost.set()
            raise _ClaimLost

    async def _run_review(
        self,
        task: _TaskContext,
        workspace: Path,
        base_sha: str,
        head_sha: str,
        material: ReviewInput,
        claim_lost: asyncio.Event,
    ):
        prompt = build_review_instructions(
            provider=task.provider,
            owner=task.owner,
            name=task.name,
            pr_number=task.pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
            material=material,
        )
        token = None
        if self.credential_broker is not None:
            token = self.credential_broker.issue(
                task_id=f"task-{task.id}",
                stage=Stage.PR_REVIEW.value,
                ttl_seconds=self.stage_timeout_seconds + 300,
            )
        try:
            spec = JobSpec(
                job_id=f"pr-review-{task.id}",
                stage=Stage.PR_REVIEW,
                role=AgentRole.PR_REVIEWER,
                workspace=workspace,
                prompt=prompt,
                timeout_seconds=self.stage_timeout_seconds,
                max_output_bytes=PR_REVIEW_MAX_OUTPUT_BYTES,
                proxy_token=token,
                review_base=material.review_base,
            )

            async def run_agent():
                return await self.runner.run(spec, cancel_event=claim_lost)

            async def restore_checkpoint() -> None:
                return None

            result = await retry_agent_operation(
                run_agent,
                restore_checkpoint,
                max_retries=2,
            )
            if result.status is not JobStatus.SUCCEEDED:
                raise PRReviewError(f"Codex 检视失败（{result.status.value}）")
            try:
                return (
                    parse_review_output(
                        result.stdout,
                        workspace=workspace,
                        ranges=material.ranges,
                        fallback_summary=(
                            f"本次 PR 涉及 {material.ranges.changed_file_count} 个变更文件，"
                            f"新增 {material.ranges.additions} 行并删除 "
                            f"{material.ranges.deletions} 行。"
                        ),
                        comparison_sha=base_sha,
                    ),
                    token,
                )
            except ReviewOutputError as exc:
                raise PRReviewError(f"Codex 检视输出无效：{exc}") from exc
        finally:
            if token is not None:
                self.credential_broker.revoke(token)

    def _load_task(self, task_id: int) -> _TaskContext:
        with self.sessions() as session:
            task = session.get(PRReviewTask, task_id)
            if task is None:
                raise PRReviewError("检视任务不存在")
            if task.review_key is None:
                raise PRReviewError("检视任务缺少幂等键")
            repository = task.repository
            return _TaskContext(
                id=task.id,
                provider=cast(ProviderName, repository.provider),
                owner=repository.owner,
                name=repository.name,
                repository_url=repository.canonical_url,
                pr_number=task.pr_number,
                source_chat_id=task.source_chat_id,
                review_key=task.review_key,
            )

    def _claim(self, task_id: int) -> str | None:
        claim_token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with self.sessions() as session:
            result = session.execute(
                update(PRReviewTask)
                .where(
                    PRReviewTask.id == task_id,
                    PRReviewTask.status == "queued",
                )
                .values(
                    status="preparing",
                    base_sha=None,
                    head_sha=None,
                    workspace_path=None,
                    structured_result=None,
                    comment_url=None,
                    failure_code=None,
                    failure_summary=None,
                    review_key=case(
                        (PRReviewTask.review_key.is_(None), secrets.token_urlsafe(32)),
                        else_=PRReviewTask.review_key,
                    ),
                    claim_token=claim_token,
                    claim_expires_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS),
                    started_at=now,
                    finished_at=None,
                )
            )
            session.commit()
            return claim_token if result.rowcount == 1 else None

    def _record_revision(
        self,
        task_id: int,
        expected: str,
        claim_token: str,
        base_sha: str,
        head_sha: str,
    ) -> None:
        self._expected_update(
            task_id,
            expected,
            claim_token,
            base_sha=base_sha,
            head_sha=head_sha,
        )

    def _transition(self, task_id: int, expected: str, status: str, claim_token: str) -> None:
        self._expected_update(task_id, expected, claim_token, status=status)

    def _set_commenting(self, task_id: int, expected: str, claim_token: str, result: dict) -> None:
        self._expected_update(
            task_id,
            expected,
            claim_token,
            status="commenting",
            structured_result=result,
        )

    def _complete(self, task_id: int, expected: str, claim_token: str, comment_url: str) -> None:
        self._expected_update(
            task_id,
            expected,
            claim_token,
            status="completed",
            comment_url=comment_url,
            claim_token=None,
            claim_expires_at=None,
            failure_code=None,
            failure_summary=None,
            finished_at=datetime.now(UTC),
        )

    def _expected_update(self, task_id: int, expected: str, owner_token: str, **values) -> None:
        with self.sessions() as session:
            result = session.execute(
                update(PRReviewTask)
                .where(
                    PRReviewTask.id == task_id,
                    PRReviewTask.status == expected,
                    PRReviewTask.claim_token == owner_token,
                )
                .values(**values)
            )
            session.commit()
            if result.rowcount != 1:
                raise _ClaimLost

    def _fail(
        self,
        task_id: int,
        expected: str,
        claim_token: str,
        exc: Exception,
        summary: str,
    ) -> bool:
        with self.sessions() as session:
            result = session.execute(
                update(PRReviewTask)
                .where(
                    PRReviewTask.id == task_id,
                    PRReviewTask.status == expected,
                    PRReviewTask.claim_token == claim_token,
                )
                .values(
                    status="failed",
                    claim_token=None,
                    claim_expires_at=None,
                    failure_code=type(exc).__name__,
                    failure_summary=summary,
                    finished_at=datetime.now(UTC),
                )
            )
            session.commit()
            return result.rowcount == 1

    async def _notify(
        self,
        task_id: int,
        chat_id: str,
        message: str,
        *,
        expected_status: str,
    ) -> None:
        if not chat_id or self.notifier is None:
            return
        try:
            await asyncio.to_thread(self.notifier.send_text, chat_id, "chat_id", message)
        except Exception as exc:
            kind = type(exc).__name__
            logger.warning("PR review notification failed: %s", kind)
            self._record_notification_failure(task_id, expected_status, kind)

    def _record_notification_failure(self, task_id: int, expected_status: str, kind: str) -> None:
        summary = f"飞书通知失败：{kind}"
        with self.sessions() as session:
            session.execute(
                update(PRReviewTask)
                .where(
                    PRReviewTask.id == task_id,
                    PRReviewTask.status == expected_status,
                )
                .values(
                    failure_summary=case(
                        (PRReviewTask.failure_summary.is_(None), summary),
                        else_=PRReviewTask.failure_summary + f"；{summary}",
                    )
                )
            )
            session.commit()

    @staticmethod
    def _validate_pr(
        details,
        *,
        expected_base_sha: str | None = None,
        expected_head_sha: str | None = None,
        expected_base_ref: str | None = None,
        expected_head_ref: str | None = None,
        expected_head_repository_url: str | None = None,
    ) -> None:
        if details.merged:
            raise PRReviewError("PR 已合并")
        if details.state != "open":
            raise PRReviewError("PR 已关闭")
        if (
            (expected_base_sha is not None and details.base_sha != expected_base_sha)
            or (expected_head_sha is not None and details.head_sha != expected_head_sha)
            or (expected_base_ref is not None and details.base_ref != expected_base_ref)
            or (expected_head_ref is not None and details.head_ref != expected_head_ref)
            or (
                expected_head_repository_url is not None
                and details.head_repository_url != expected_head_repository_url
            )
        ):
            raise PRReviewError("PR 版本已变化")

    @staticmethod
    def _redact_output(
        output: ReviewOutput, proxy_token: str | None, workspace: Path
    ) -> ReviewOutput:
        absolute = str(Path(workspace).resolve())
        sensitive = {
            value
            for value in (
                proxy_token,
                absolute,
                absolute.replace("\\", "/"),
                absolute.replace("/", "\\"),
            )
            if value
        }

        def redact(value):
            if isinstance(value, str):
                return PRReviewOrchestrator._redact_text(value, sensitive)
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value

        return ReviewOutput.model_validate(redact(output.model_dump(mode="json")))

    @staticmethod
    def _redact_text(value: str, sensitive: set[str]) -> str:
        for secret in sorted(sensitive, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    @staticmethod
    def _safe_summary(exc: Exception) -> str:
        if isinstance(exc, (PRReviewError, ReviewOutputError)):
            return str(exc)[:500]
        return type(exc).__name__
