from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from coderus.forge import ForgeRegistry, ProviderName, PullRequestDetails, Repository
from coderus.model_proxy import issued_stage_token
from coderus.models import PRReviewTask
from coderus.pr_review.history import (
    REVIEW_HISTORY_VERSION,
    PreviousReview,
    TrackedFinding,
    apply_rechecks,
    scope_digest,
)
from coderus.pr_review.instructions import ReviewAxis, build_review_instructions
from coderus.pr_review.models import FindingRecheck, ReviewFinding, ReviewInput, ReviewOutput
from coderus.pr_review.result import (
    ReviewOutputError,
    parse_review_output,
    render_pr_comment,
    review_output_schema_path,
    unlocated_findings,
    validate_findings,
)
from coderus.pr_review.spec_source import SpecSource, resolve_spec_source
from coderus.pr_review.upstream_errors import upstream_error_message
from coderus.pr_review.workspace import PRWorkspaceError, RevisionChanged
from coderus.runner import AgentRole, JobSpec, JobStatus, Stage
from coderus.tasks.error_codes import TaskErrorCode, classify_exception
from coderus.tasks.lease import TaskLease, heartbeat_loop
from coderus.workflow.limited_runner import retry_agent_operation

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ("preparing", "reviewing", "commenting")
CLAIM_LEASE_SECONDS = 120.0
CLAIM_HEARTBEAT_SECONDS = 10.0
COMMENT_TIMEOUT_SECONDS = 90.0
PR_REVIEW_MAX_OUTPUT_BYTES = 5_000_000
# 代理进程非零退出多为模型接口瞬时故障，整体作业最多执行两次。
AGENT_JOB_ATTEMPTS = 2
UPSTREAM_MAX_RETRIES = 3
UPSTREAM_RETRY_SECONDS = 300.0
LGTM_COMMENT = "/lgtm"


class Runner(Protocol):
    async def run(self, spec: JobSpec, *, cancel_event=None): ...


class PRReviewError(RuntimeError):
    """A controlled error safe to persist and send to the requester."""

    error_code = TaskErrorCode.INVALID_INPUT


class ReviewAgentFailure(PRReviewError):
    """检视代理进程异常退出；消息中带脱敏后的诊断尾部。"""

    error_code = TaskErrorCode.INTERNAL_ERROR


class ReviewUpstreamFailure(PRReviewError):
    """模型上游请求失败；仅向用户发送已脱敏的信息。"""

    error_code = TaskErrorCode.UPSTREAM_UNAVAILABLE


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
        model_secrets: tuple[str, ...] = (),
        providers: Mapping[str, object] | None = None,
        review_model: str | None = None,
    ) -> None:
        self.sessions = session_factory
        self.forges = forges
        self.runner = runner
        self.workspace = workspace
        self.notifier = notifier
        self.credential_broker = credential_broker
        self.stage_timeout_seconds = stage_timeout_seconds
        self._model_secrets = model_secrets
        self.providers = providers if providers is not None else {}
        self.review_model = review_model

    async def run(self, task_id: int) -> None:
        claim_token = self._claim(task_id)
        if claim_token is None:
            return
        heartbeat_stop = threading.Event()
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
                live_head = await self.workspace.resolve_remote_head(
                    details.head_repository_url, details.head_ref
                )
                if live_head is not None and live_head != details.head_sha:
                    # 平台 API 的 PR 元数据可能滞后于源分支的实际推送，
                    # 检视必须以分支当前 tip 为准。
                    details = replace(details, head_sha=live_head)
                self._record_revision(
                    task_id,
                    state,
                    claim_token,
                    details.base_sha,
                    details.head_sha,
                )
                self._validate_pr(details)

                self._assert_claim(task_id, claim_token, claim_lost)
                for attempt in range(3):
                    try:
                        checkout = await self.workspace.prepare(
                            task_id=task.id,
                            repository_url=task.repository_url,
                            pr_number=task.pr_number,
                            base_ref=details.base_ref,
                            head_sha=details.head_sha,
                            head_ref=details.head_ref,
                            head_repository_url=details.head_repository_url,
                        )
                        break
                    except RevisionChanged:
                        if attempt == 2:
                            raise
                        self._assert_claim(task_id, claim_token, claim_lost)
                        live_head = await self.workspace.resolve_remote_head(
                            details.head_repository_url, details.head_ref
                        )
                        if live_head is None:
                            raise
                        details = replace(details, head_sha=live_head)
                        self._record_revision(
                            task_id, state, claim_token, details.base_sha, details.head_sha
                        )
                prepared = checkout.path
                details = replace(details, base_sha=checkout.base_sha)
                self._record_revision(
                    task_id, state, claim_token, details.base_sha, details.head_sha,
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
                spec_source = await resolve_spec_source(
                    material,
                    repository=Repository(
                        task.provider, task.owner, task.name, task.repository_url
                    ),
                    provider=self.providers.get(task.provider),
                    head_ref=details.head_ref,
                    pr_body=details.body,
                )
                self._assert_claim(task_id, claim_token, claim_lost)
                previous = self._previous_review(task_id)
                scope = await self._review_scope(
                    prepared, details, ranges.comparison_sha, spec_source,
                )
                matches_scope = (
                    previous is not None and scope is not None and previous.scope == scope
                )
                if matches_scope and previous is not None and previous.head_sha == details.head_sha:
                    await self._revalidate_publication(
                        task, prepared, details, ranges.comparison_sha,
                    )
                    self._assert_claim(task_id, claim_token, claim_lost)
                    self._complete(
                        task_id, state, claim_token, previous.comment_url,
                        structured_result=previous.reuse_result(),
                    )
                    await self._notify(
                        task.id,
                        task.source_chat_id,
                        f"RV-{task.id} 当前版本已检视，沿用结果：{previous.comment_url}",
                        expected_status="completed",
                    )
                    return
                previous_head = None
                if matches_scope and previous is not None and await self.workspace.can_follow_up(
                    prepared, previous.head_sha, details.head_sha,
                ):
                    previous_head = previous.head_sha
                reports = await self._run_review_axes(
                    task, prepared, ranges.comparison_sha, details.head_sha,
                    material, claim_lost, spec_source, previous, previous_head,
                )
                self._assert_claim(task_id, claim_token, claim_lost)
                await self.workspace.assert_pristine(
                    prepared,
                    details.head_sha,
                    ranges.comparison_sha,
                )
                self._assert_claim(task_id, claim_token, claim_lost)
                review_secrets = self._model_secrets + tuple(token for _, token in reports if token)
                sensitive = self._sensitive_values(prepared, review_secrets)
                prior = previous.open_findings if previous else ()
                reviews = [
                    self._without_repeats(axis, report, prior)
                    for axis, (report, _) in zip(("standards", "spec"), reports, strict=False)
                ]
                rechecks = [check for report in reviews for check in report._rechecks]
                checked = [
                    (item, check.model_copy(update={
                        "reason": self._redact_text(check.reason, sensitive),
                    }))
                    for item, check in apply_rechecks(prior, rechecks)
                ]
                validated = [validate_findings(report, ranges) for report in reviews]
                generated_count = sum(len(report.findings) for report in reviews)
                output = validated[0].model_copy(update={
                    "findings": [finding for report in validated for finding in report.findings],
                })
                standards_count = len(validated[0].findings)
                pending_findings = []
                for axis, report in zip(("standards", "spec"), reviews, strict=False):
                    pending_findings.extend(
                        {**item, "review_axis": axis} for item in unlocated_findings(report, ranges)
                    )
                filtered_finding_count = len(pending_findings)

                self._assert_claim(task_id, claim_token, claim_lost)
                publication_base = await self._revalidate_publication(
                    task, prepared, details, ranges.comparison_sha,
                )
                output = self._redact_output(output, None, prepared, review_secrets)
                for item in pending_findings:
                    pending_output = ReviewOutput(
                        change_summary=output.change_summary, findings=[item["finding"]],
                    )
                    safe = self._redact_output(
                        pending_output, None, prepared, review_secrets,
                    )
                    item["finding"] = safe.findings[0].model_dump(mode="json")
                structured_result = output.model_dump(mode="json")
                structured_result["unlocated_findings"] = pending_findings
                structured_result["review_axes"] = {
                    "standards": {"status": "completed", "validated_findings": standards_count},
                    "spec": {
                        "status": "completed" if spec_source else "skipped_no_spec",
                        "validated_findings": len(output.findings) - standards_count,
                    },
                }
                structured_result["review_audit"] = {
                    "comparison_sha": ranges.comparison_sha,
                    "changed_files": ranges.changed_file_count,
                    "additions": ranges.additions,
                    "deletions": ranges.deletions,
                    "review_mode": "matt_pocock_code_review",
                    "generated_findings": generated_count,
                    "validated_findings": len(output.findings),
                    "filtered_findings": filtered_finding_count,
                }
                if publication_base != details.base_sha:
                    structured_result["review_audit"]["publication_base_sha"] = publication_base
                new_findings = [
                    ("standards" if index < standards_count else "spec", finding)
                    for index, finding in enumerate(output.findings)
                ]
                new_findings.extend(
                    (item["review_axis"], ReviewFinding.model_validate(item["finding"]))
                    for item in pending_findings
                )
                tracked = [
                    TrackedFinding(
                        id=f"RV-{task_id}-{index}",
                        axis=axis,
                        head_sha=details.head_sha,
                        comparison_sha=ranges.comparison_sha,
                        finding=finding,
                    )
                    for index, (axis, finding) in enumerate(new_findings, 1)
                ]
                outstanding = [item for item, check in checked if check.status != "resolved"]
                structured_result["review_history"] = {
                    "version": REVIEW_HISTORY_VERSION,
                    "mode": "followup" if previous_head else "full",
                    "scope": scope,
                    "previous_task_id": previous.task_id if previous else None,
                    "open_findings": [
                        item.model_dump(mode="json") for item in [*outstanding, *tracked]
                    ],
                    "rechecks": [check.model_dump(mode="json") for _, check in checked],
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
                    pending_findings=pending_findings,
                    standards_count=standards_count,
                    spec_reviewed=spec_source is not None,
                    previous=previous,
                    checked=checked,
                    incremental=previous_head is not None,
                )
                self._assert_claim(task_id, claim_token, claim_lost)
                comment = await asyncio.wait_for(
                    self.forges.require(task.provider).publish_pr_comment(
                        task.owner, task.name, task.pr_number, body, marker
                    ),
                    timeout=COMMENT_TIMEOUT_SECONDS,
                )
                if not generated_count and not outstanding:
                    lgtm_marker = (
                        f"<!-- coderus-pr-review-lgtm:{details.head_sha} -->"
                    )
                    self._assert_claim(task_id, claim_token, claim_lost)
                    await asyncio.wait_for(
                        self.forges.require(task.provider).publish_pr_comment(
                            task.owner,
                            task.name,
                            task.pr_number,
                            f"{LGTM_COMMENT}\n{lgtm_marker}",
                            lgtm_marker,
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
        stop: threading.Event,
        claim_lost: asyncio.Event,
    ) -> None:
        await heartbeat_loop(
            lambda: self._renew_claim(task_id, claim_token),
            stop,
            claim_lost.set,
            interval=CLAIM_HEARTBEAT_SECONDS,
            lease_seconds=CLAIM_LEASE_SECONDS,
            log_label=f"pr-review-{task_id}",
        )

    def _renew_claim(self, task_id: int, claim_token: str) -> bool:
        # 每次调用重建租约对象，让测试可以在运行期调小 CLAIM_LEASE_SECONDS。
        lease = TaskLease(
            session_factory=self.sessions,
            model=PRReviewTask,
            active_statuses=ACTIVE_STATUSES,
            lease_seconds=CLAIM_LEASE_SECONDS,
        )
        return lease.renew(task_id, claim_token)

    def _assert_claim(self, task_id: int, claim_token: str, claim_lost: asyncio.Event) -> None:
        if claim_lost.is_set() or not self._renew_claim(task_id, claim_token):
            claim_lost.set()
            raise _ClaimLost

    def _previous_review(self, task_id: int) -> PreviousReview | None:
        with self.sessions() as session:
            task = session.get(PRReviewTask, task_id)
            row = session.execute(
                select(
                    PRReviewTask.id, PRReviewTask.head_sha, PRReviewTask.comment_url,
                    PRReviewTask.structured_result,
                )
                .where(
                    PRReviewTask.repository_id == task.repository_id,
                    PRReviewTask.pr_number == task.pr_number,
                    PRReviewTask.id != task_id,
                    PRReviewTask.status == "completed",
                )
                .order_by(PRReviewTask.finished_at.desc(), PRReviewTask.id.desc())
                .limit(1)
            ).first()
        return PreviousReview.load(*row) if row is not None else None

    async def _review_scope(
        self, workspace: Path, details: PullRequestDetails, comparison_sha: str,
        spec_source: SpecSource | None,
    ) -> str | None:
        spec_reference = spec_value = ""
        if spec_source is not None:
            spec_reference = spec_source.reference
            if spec_source.text is not None:
                spec_value = spec_source.text
            else:
                oid = await self.workspace.review_document_oid(
                    workspace, details.head_sha, spec_reference,
                )
                if oid is None:
                    return None
                spec_value = oid
        return scope_digest(
            str(REVIEW_HISTORY_VERSION), self.review_model or "", comparison_sha, details.base_ref,
            details.head_ref, details.head_repository_url, spec_reference, spec_value,
        )

    @staticmethod
    def _without_repeats(
        axis: str, report: ReviewOutput, prior: tuple[TrackedFinding, ...],
    ) -> ReviewOutput:
        repeated = set()
        fresh = []
        for finding in report.findings:
            matches = [item for item in prior if item.axis == axis and item.matches(finding)]
            if len(matches) == 1:
                repeated.add(matches[0].id)
            else:
                fresh.append(finding)
        if not repeated:
            return report
        output = report.model_copy(update={"findings": fresh})
        output._rechecks = [
            FindingRecheck(
                id=check.id, status="needs_confirmation",
                reason="本轮又报告了同一问题，需确认是否已解决。",
            )
            if check.id in repeated and check.status == "resolved"
            else check
            for check in report._rechecks
        ]
        return output

    async def _revalidate_publication(
        self, task: _TaskContext, workspace: Path, details: PullRequestDetails, comparison_sha: str,
    ) -> str:
        current = await self.forges.require(task.provider).get_pull_request(
            task.owner, task.name, task.pr_number,
        )
        self._validate_pr(
            current,
            expected_base_ref=details.base_ref,
            expected_head_ref=details.head_ref,
            expected_head_repository_url=details.head_repository_url,
        )
        current_head = await self.workspace.resolve_remote_head(
            details.head_repository_url, details.head_ref,
        )
        if current_head is not None and current_head != details.head_sha:
            raise PRReviewError("检视期间源分支有新提交，请重新发起检视")
        if current_head is None and current.head_sha != details.head_sha:
            raise PRReviewError("无法确认源分支仍是已检视的版本，请重新发起检视")
        current_base = await self.workspace.resolve_remote_head(
            task.repository_url, details.base_ref,
        )
        if (current_base or current.base_sha) == details.base_sha:
            return details.base_sha
        return await self._verify_target_update(task, workspace, details, comparison_sha)

    async def _verify_target_update(
        self, task: _TaskContext, workspace: Path, details: PullRequestDetails, comparison_sha: str,
    ) -> str:
        refreshed = await self.workspace.refresh_target_comparison(
            workspace, task.repository_url, details.base_ref, details.head_sha,
        )
        if refreshed is None:
            raise PRReviewError("目标分支已更新，但无法核对检视基准，请重新发起检视")
        target_sha, new_comparison = refreshed
        if new_comparison != comparison_sha:
            raise PRReviewError("目标分支更新改变了检视基准，请重新发起检视")

        # Fetching the target introduces another window in which the source can move.
        try:
            async with asyncio.timeout(15):
                head = await self.workspace.resolve_remote_head(
                    details.head_repository_url, details.head_ref,
                )
        except TimeoutError:
            head = None
        if head is None:
            raise PRReviewError("无法确认源分支仍是已检视的版本，请重新发起检视")
        if head != details.head_sha:
            raise PRReviewError("检视期间源分支有新提交，请重新发起检视")
        return target_sha

    async def _run_review_axes(
        self,
        task: _TaskContext,
        workspace: Path,
        base_sha: str,
        head_sha: str,
        material: ReviewInput,
        claim_lost: asyncio.Event,
        spec_source: SpecSource | None,
        previous: PreviousReview | None,
        previous_head: str | None,
    ) -> list[tuple[ReviewOutput, str | None]]:
        async def review(axis: ReviewAxis, source: SpecSource | None = None):
            return await self._run_review(
                task, workspace, base_sha, head_sha, material, claim_lost, axis, source,
                previous.for_axis(axis) if previous else (), previous_head,
            )

        if spec_source is None:
            return [await review("standards")]
        jobs = [
            asyncio.create_task(review("standards")),
            asyncio.create_task(review("spec", spec_source)),
        ]
        try:
            return await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _run_review(
        self,
        task: _TaskContext,
        workspace: Path,
        base_sha: str,
        head_sha: str,
        material: ReviewInput,
        claim_lost: asyncio.Event,
        axis: ReviewAxis,
        spec_source: SpecSource | None,
        previous_findings: tuple[TrackedFinding, ...],
        previous_head: str | None,
    ) -> tuple[ReviewOutput, str | None]:
        prompt = build_review_instructions(
            provider=task.provider,
            owner=task.owner,
            name=task.name,
            pr_number=task.pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
            material=material,
            axis=axis,
            previous_head=previous_head,
            recheck_previous=bool(previous_findings),
        )
        context = spec_source.prompt() if spec_source else None
        if previous_findings:
            history = json.dumps(
                {"previous_findings": [item.prompt_data() for item in previous_findings]},
                ensure_ascii=False,
            )
            context = f"{context}\n{history}" if context else history
        with issued_stage_token(
            self.credential_broker,
            task_id=f"task-{task.id}",
            stage=Stage.PR_REVIEW.value,
            ttl_seconds=(
                self.stage_timeout_seconds * (UPSTREAM_MAX_RETRIES + 1)
                + UPSTREAM_RETRY_SECONDS * UPSTREAM_MAX_RETRIES + 300
            ),
        ) as token:
            spec = JobSpec(
                job_id=f"pr-review-{task.id}-{axis}",
                stage=Stage.PR_REVIEW,
                role=AgentRole.PR_REVIEWER,
                workspace=workspace,
                prompt=prompt,
                timeout_seconds=self.stage_timeout_seconds,
                max_output_bytes=PR_REVIEW_MAX_OUTPUT_BYTES,
                output_schema=review_output_schema_path(),
                proxy_token=token,
                review_base=material.review_base,
                review_context=context,
            )

            async def run_agent():
                if token and self.credential_broker:
                    self.credential_broker.take_upstream_error(token)
                return await self.runner.run(spec, cancel_event=claim_lost)

            async def restore_checkpoint() -> None:
                return None

            for attempt in range(1, UPSTREAM_MAX_RETRIES + 2):
                if claim_lost.is_set():
                    raise _ClaimLost
                result = await retry_agent_operation(
                    run_agent,
                    restore_checkpoint,
                    max_retries=2,
                )
                if result.status is JobStatus.SUCCEEDED:
                    break
                # 诊断细节只进服务端日志；摘要会同步到飞书，不携带代理原始输出。
                logger.error(
                    "pr-review-%s agent job failed (attempt %s/%s, %s, exit_code=%s): %s",
                    task.id,
                    attempt,
                    UPSTREAM_MAX_RETRIES + 1,
                    result.status.value,
                    result.exit_code,
                    self._agent_failure_detail(result, token, workspace),
                )
                upstream = upstream_error_message(
                    result, proxy_token=token, workspace=workspace,
                    known_secrets=self._model_secrets,
                )
                if token and self.credential_broker:
                    upstream = self.credential_broker.take_upstream_error(token) or upstream
                if result.status is JobStatus.FAILED and upstream is not None:
                    if attempt == UPSTREAM_MAX_RETRIES + 1:
                        raise ReviewUpstreamFailure(
                            f"上游模型错误：{upstream}；已重试 {UPSTREAM_MAX_RETRIES} 次仍失败，"
                            "本任务已结束，请重新发送“检视 <PR URL>”触发任务。"
                        )
                    if claim_lost.is_set():
                        raise _ClaimLost
                    await self._notify(
                        task.id, task.source_chat_id,
                        f"RV-{task.id} 上游模型错误：{upstream}。"
                        f"将在 5 分钟后进行第 {attempt}/{UPSTREAM_MAX_RETRIES} 次重试。",
                        expected_status="reviewing",
                    )
                    await self._wait_upstream_retry(claim_lost)
                    continue
                # 仅对非零退出重试一次；超时重试会翻倍耗时，取消意味着任务权已丢失。
                if result.status is not JobStatus.FAILED or attempt >= AGENT_JOB_ATTEMPTS:
                    raise ReviewAgentFailure(f"Codex 检视失败（{result.status.value}）")
            try:
                if result.output_truncated:
                    raise ReviewOutputError("检视输出被截断")
                return (
                    parse_review_output(
                        result.stdout, comparison_sha=previous_head or base_sha,
                        incremental=previous_head is not None,
                        previous_ids=tuple(item.id for item in previous_findings),
                    ),
                    token,
                )
            except ReviewOutputError as exc:
                raise PRReviewError(f"Codex 检视输出无效：{exc}") from exc

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

    def _complete(
        self, task_id: int, expected: str, claim_token: str, comment_url: str,
        *, structured_result: dict | None = None,
    ) -> None:
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
            **({"structured_result": structured_result} if structured_result is not None else {}),
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
                    failure_code=classify_exception(exc),
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
        details: PullRequestDetails,
        *,
        expected_base_ref: str | None = None,
        expected_head_ref: str | None = None,
        expected_head_repository_url: str | None = None,
    ) -> None:
        if details.merged:
            raise PRReviewError("PR 已合并")
        if details.state != "open":
            raise PRReviewError("PR 已关闭")
        if expected_base_ref is not None and details.base_ref != expected_base_ref:
            raise PRReviewError("PR 的目标分支已切换，请重新发起检视")
        if (
            (expected_head_ref is not None and details.head_ref != expected_head_ref)
            or (
                expected_head_repository_url is not None
                and details.head_repository_url != expected_head_repository_url
            )
        ):
            raise PRReviewError("PR 的源分支或来源仓库已切换，请重新发起检视")

    @staticmethod
    def _redact_output(
        output: ReviewOutput, proxy_token: str | None, workspace: Path,
        extra_secrets: tuple[str, ...] = (),
    ) -> ReviewOutput:
        sensitive = PRReviewOrchestrator._sensitive_values(workspace, extra_secrets)
        if proxy_token:
            sensitive.add(proxy_token)

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
    def _sensitive_values(workspace: Path, secrets: tuple[str, ...]) -> set[str]:
        absolute = str(workspace.resolve())
        return {
            value for value in (*secrets, absolute, absolute.replace("\\", "/"),
                                absolute.replace("/", "\\")) if value
        }

    @staticmethod
    async def _wait_upstream_retry(claim_lost: asyncio.Event) -> None:
        """Wait without blocking the lease heartbeat; stop immediately on claim loss."""
        try:
            await asyncio.wait_for(claim_lost.wait(), timeout=UPSTREAM_RETRY_SECONDS)
        except TimeoutError:
            return
        raise _ClaimLost

    @staticmethod
    def _agent_failure_detail(result, proxy_token: str | None, workspace: Path) -> str:
        """提取代理失败输出的脱敏尾部，仅写入服务端日志。

        ``--json`` 模式下真实错误事件在 stdout，stderr 往往只有启动提示，
        因此两路输出都要保留。
        """
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
        parts = []
        for label, stream in (("stderr", result.stderr), ("stdout", result.stdout)):
            text = stream.strip()
            if not text:
                continue
            cleaned = PRReviewOrchestrator._redact_text(text, sensitive)
            parts.append(f"{label}: {' '.join(cleaned[-400:].split())}")
        return " | ".join(parts) or "代理进程未输出诊断信息"

    @staticmethod
    def _redact_text(value: str, sensitive: set[str]) -> str:
        for secret in sorted(sensitive, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    @staticmethod
    def _safe_summary(exc: Exception) -> str:
        if isinstance(exc, (PRReviewError, ReviewOutputError, PRWorkspaceError)):
            return str(exc)[:500]
        return type(exc).__name__
