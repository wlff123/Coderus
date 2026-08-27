from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.models import AgentRun, Issue, PRFeedback, Review, Task
from coderus.runner import AgentRole, JobStatus, Stage
from coderus.tasks.statuses import TERMINAL_TASK_STATES
from coderus.workflow.agent_stage import (
    AgentStageExecutor,
    TaskCancelled,
    final_message,
)
from coderus.workflow.developer_report import (
    DeveloperReport,
    DeveloperReportError,
    parse_developer_report,
    render_developer_report,
)
from coderus.workflow.prompts import (
    developer_prompt,
    feedback_prompt,
    pull_request_body,
    review_prompt,
    revision_prompt,
)
from coderus.workflow.reviewer_result import (
    REVIEWER_CONTRACT_VERSION,
    ReviewerResultError,
    parse_reviewer_result,
)
from coderus.workflow.task_state import cas_task_status, claim_queued_task

CLAIM_LEASE_SECONDS = 120.0
CLAIM_HEARTBEAT_SECONDS = 10.0

_STATUS_EXPECTED = {
    "preparing": {"queued"},
    "developer_working": {"preparing"},
    "reviewing": {"developer_working", "reviewing"},
    "developer_revising": {"preparing", "reviewing"},
    "sealing": {"preparing", "developer_working", "reviewing", "developer_revising"},
    "publishing": {"preparing", "sealing"},
}
_CLAIMED_STATES = (*_STATUS_EXPECTED, "cancelling")


class Runner(Protocol):
    async def run(self, spec, *, cancel_event: asyncio.Event): ...


class _ClaimLost(Exception):
    pass


class TaskOrchestrator:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        runner: Runner,
        workspace_git: object,
        forges: ForgeRegistry,
        artifacts_root: Path,
        git_user_name: str,
        git_user_email: str,
        stage_timeout_seconds: int = 3600,
        notifier: object | None = None,
        credential_broker: object | None = None,
    ) -> None:
        self.sessions = session_factory
        self.runner = runner
        self.git = workspace_git
        self.forges = forges
        self.artifacts_root = artifacts_root
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.stage_timeout_seconds = stage_timeout_seconds
        self.notifier = notifier
        self.credential_broker = credential_broker
        self._cancel_events: dict[int, asyncio.Event] = {}
        self.stage_executor = AgentStageExecutor(
            session_factory=session_factory,
            runner=runner,
            credential_broker=credential_broker,
            stage_timeout_seconds=stage_timeout_seconds,
            transition=self._set_status,
        )

    def cancel(self, task_id: int) -> bool:
        event = self._cancel_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def _claim(self, task_id: int) -> str | None:
        with self.sessions() as session:
            token = claim_queued_task(
                session,
                task_id,
                global_limit=2**31 - 1,
                per_user_limit=2**31 - 1,
                lease_seconds=CLAIM_LEASE_SECONDS,
            )
            session.commit()
            return token

    async def _heartbeat(
        self,
        task_id: int,
        claim_token: str,
        stop: asyncio.Event,
        claim_lost: asyncio.Event,
        cancel_event: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=CLAIM_HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                try:
                    renewed = self._renew_claim(task_id, claim_token)
                except Exception:
                    renewed = False
                if not renewed:
                    claim_lost.set()
                    cancel_event.set()
                    return

    def _renew_claim(self, task_id: int, claim_token: str) -> bool:
        now = datetime.now(UTC)
        with self.sessions() as session:
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status.in_(_CLAIMED_STATES),
                    Task.claim_token == claim_token,
                    Task.claim_expires_at > now,
                )
                .values(
                    claim_expires_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS)
                )
            )
            session.commit()
            return result.rowcount == 1

    async def run(self, task_id: int, *, claim_token: str | None = None) -> None:
        if claim_token is None:
            claim_token = self._claim(task_id)
        elif not self._renew_claim(task_id, claim_token):
            return
        if claim_token is None:
            return
        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event
        heartbeat_stop = asyncio.Event()
        claim_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(
                task_id,
                claim_token,
                heartbeat_stop,
                claim_lost,
                cancel_event,
            ),
            name=f"issue-task-heartbeat-{task_id}",
        )
        try:
            task = self._load_task(task_id)
            if task.failure_code == "pr_feedback_revision":
                await self._run_feedback_revision(task, claim_token)
                return
            if task.failure_code == "publish_existing":
                await self._run_existing_publish(task, claim_token)
                return
            branch = f"coderus/issue-{task.issue.number}-{task.id}"
            prepared = await self.git.prepare(
                task.id,
                task.issue.repository.canonical_url,
                task.issue.repository.default_branch,
                branch,
            )
            self._set_prepared(task_id, claim_token, prepared)

            developer = await self._run_stage(
                task.id,
                "developer_working",
                Stage.DEVELOP,
                AgentRole.DEVELOPER,
                prepared.workspace,
                developer_prompt(task),
                claim_token,
            )
            reports = [parse_developer_report(final_message(developer.stdout))]
            await self.git.assert_has_changes(prepared.workspace)

            findings = await self._run_reviewers(
                task,
                prepared.workspace,
                render_developer_report(reports[-1]),
                claim_token,
            )
            if findings:
                revision = await self._run_stage(
                    task.id,
                    "developer_revising",
                    Stage.REVISE,
                    AgentRole.DEVELOPER,
                    prepared.workspace,
                    revision_prompt(task, findings),
                    claim_token,
                )
                reports.append(parse_developer_report(final_message(revision.stdout)))
                await self.git.assert_has_changes(prepared.workspace)

            await self._finalize(
                task,
                prepared.workspace,
                branch,
                reports,
                findings,
                patch_name="fixed.patch",
                commit_title=f"Fix #{task.issue.number}: {task.issue.title}"[:200],
                claim_token=claim_token,
            )
        except _ClaimLost:
            return
        except TaskCancelled:
            self._mark_cancelled(task_id, claim_token)
        except asyncio.CancelledError:
            self._interrupt(task_id, claim_token)
            raise
        except DeveloperReportError as exc:
            self._manual_intervention(
                task_id,
                claim_token,
                "developer_report_invalid",
                str(exc)[-2000:],
            )
        except Exception as exc:
            self._fail(task_id, claim_token, exc)
        finally:
            heartbeat_stop.set()
            await heartbeat
            self._cancel_events.pop(task_id, None)

    async def _run_feedback_revision(self, task: Task, claim_token: str) -> None:
        if not task.workspace_path or not task.branch_name or not task.pr_number:
            raise ValueError("任务缺少可继续处理 PR 意见的工作区或分支")
        workspace = Path(task.workspace_path).resolve()
        if not workspace.is_dir():
            raise ValueError("任务工作区不存在")
        feedback, feedback_ids = self._selected_feedback(task.id)
        if not feedback:
            raise ValueError("没有已选择的 PR 意见")
        await self.git.assert_branch(workspace, task.branch_name)
        revision = await self._run_stage(
            task.id,
            "developer_revising",
            Stage.REVISE,
            AgentRole.DEVELOPER,
            workspace,
            feedback_prompt(task, feedback),
            claim_token,
        )
        report = parse_developer_report(final_message(revision.stdout))
        await self._finalize(
            task,
            workspace,
            task.branch_name,
            [report],
            [],
            patch_name="pr-feedback.patch",
            commit_title=f"Address PR feedback for #{task.issue.number}"[:200],
            claim_token=claim_token,
        )
        self._mark_feedback_processed(task.id, feedback_ids)

    async def _run_existing_publish(self, task: Task, claim_token: str) -> None:
        if not task.workspace_path or not task.branch_name:
            raise ValueError("任务缺少可发布的工作区或分支")
        workspace = Path(task.workspace_path).resolve()
        if not workspace.is_dir():
            raise ValueError("任务工作区不存在")
        await self.git.assert_branch(workspace, task.branch_name)
        reports, findings = self._existing_reports_and_findings(task.id)
        if task.commit_sha:
            await self.git.assert_clean_commit(workspace, task.commit_sha)
            published = await self._publish(
                task, workspace, task.branch_name, reports, findings, claim_token
            )
            self._awaiting_review(task.id, claim_token, published)
            await self._notify(task, published)
            return
        await self._finalize(
            task,
            workspace,
            task.branch_name,
            reports,
            findings,
            patch_name="fixed.patch",
            commit_title=f"Fix #{task.issue.number}: {task.issue.title}"[:200],
            claim_token=claim_token,
        )

    def _existing_reports_and_findings(
        self, task_id: int
    ) -> tuple[list[DeveloperReport], list[dict[str, Any]]]:
        with self.sessions() as session:
            runs = session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.task_id == task_id,
                    AgentRun.role == AgentRole.DEVELOPER.value,
                    AgentRun.status == JobStatus.SUCCEEDED.value,
                )
                .order_by(AgentRun.id.desc())
                .limit(1)
            ).all()
            reviews = session.scalars(
                select(Review).where(Review.task_id == task_id).order_by(Review.id)
            ).all()
        reports = []
        for run in runs:
            if not isinstance(run.structured_result, dict):
                continue
            payload = run.structured_result.get("developer_report")
            if payload is not None:
                reports.append(
                    parse_developer_report(json.dumps(payload, ensure_ascii=False))
                )
        if not reports:
            raise DeveloperReportError("历史任务缺少结构化开发报告，无法发布")
        latest_reviews = {review.reviewer_role: review for review in reviews}
        findings = deduplicate_findings(
            [finding for review in latest_reviews.values() for finding in review.findings]
        )
        return reports, findings

    async def _finalize(
        self,
        task: Task,
        workspace: Path,
        branch: str,
        reports: list[DeveloperReport],
        findings: list[dict[str, Any]],
        *,
        patch_name: str,
        commit_title: str,
        claim_token: str,
    ) -> None:
        await self.git.assert_has_changes(workspace)
        self._set_status(task.id, "sealing", claim_token)
        patch_path = self.artifacts_root / f"task-{task.id}" / patch_name
        sealed = await self.git.seal(workspace, patch_path)
        self._set_sealed(task.id, claim_token, sealed)
        await self.git.assert_no_secrets(workspace)
        await self.git.assert_tree(workspace, sealed.tree_sha)
        commit_sha = await self.git.commit(
            workspace, commit_title, self.git_user_name, self.git_user_email
        )
        await self.git.assert_committed_tree(workspace, commit_sha, sealed.tree_sha)
        self._set_commit(task.id, claim_token, commit_sha)
        published = await self._publish(
            task, workspace, branch, reports, findings, claim_token
        )
        self._awaiting_review(task.id, claim_token, published)
        await self._notify(task, published)

    def _selected_feedback(
        self, task_id: int
    ) -> tuple[list[dict[str, Any]], tuple[int, ...]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(PRFeedback)
                .where(
                    PRFeedback.task_id == task_id,
                    PRFeedback.selected_at.is_not(None),
                    PRFeedback.processed_at.is_(None),
                )
                .order_by(PRFeedback.id)
            ).all()
            feedback = [
                {
                    "author": row.author,
                    "kind": row.kind,
                    "path": row.path,
                    "line": row.line,
                    "body": row.body,
                }
                for row in rows
            ]
            return feedback, tuple(row.id for row in rows)

    def _mark_feedback_processed(
        self, task_id: int, feedback_ids: tuple[int, ...]
    ) -> None:
        if not feedback_ids:
            return
        with self.sessions() as session:
            rows = session.scalars(
                select(PRFeedback).where(
                    PRFeedback.task_id == task_id,
                    PRFeedback.id.in_(feedback_ids),
                    PRFeedback.selected_at.is_not(None),
                    PRFeedback.processed_at.is_(None),
                )
            ).all()
            now = datetime.now(UTC)
            for row in rows:
                row.processed_at = now
            session.commit()

    async def _run_reviewers(
        self,
        task: Task,
        workspace: Path,
        developer_report: str,
        claim_token: str,
    ) -> list[dict[str, Any]]:
        specs = (
            (Stage.REVIEW_CORRECTNESS, AgentRole.REVIEWER_A, "正确性、回归风险和测试覆盖"),
            (Stage.REVIEW_SECURITY, AgentRole.REVIEWER_B, "安全性、边界条件和可维护性"),
        )
        reviewer_tasks: list[asyncio.Task] = []
        async with asyncio.TaskGroup() as group:
            for stage, role, focus in specs:
                reviewer_tasks.append(
                    group.create_task(
                        self._run_stage(
                            task.id,
                            "reviewing",
                            stage,
                            role,
                            workspace,
                            review_prompt(task, focus, developer_report),
                            claim_token,
                        )
                    )
                )
        results = [reviewer_task.result() for reviewer_task in reviewer_tasks]
        findings: list[dict[str, Any]] = []
        for (_, role, _), result in zip(specs, results, strict=True):
            decision, role_findings = review_result(result.stdout)
            self._record_review(task.id, role.value, decision, role_findings)
            if decision != "approve":
                findings.extend(role_findings)
        return deduplicate_findings(findings)

    async def _publish(
        self,
        task: Task,
        workspace: Path,
        branch: str,
        reports: list[DeveloperReport],
        findings: list[dict[str, Any]],
        claim_token: str,
    ):
        provider = task.issue.repository.provider
        forge = self.forges.require(provider)
        if not self.forges.supports(provider, ForgeCapability.PUBLISH):
            raise ValueError("未配置代码平台发布凭据")
        publication_key = self._begin_publication(task.id, claim_token)
        body = pull_request_body(task, reports)
        body += f"\n\n<!-- coderus-publication:{publication_key} -->"
        published = await forge.publish(
            workspace=workspace,
            upstream_owner=task.issue.repository.owner,
            repository_name=task.issue.repository.name,
            default_branch=task.issue.repository.default_branch,
            branch=branch,
            title=task.issue.title[:240],
            body=body,
        )
        if inspect.isawaitable(published):
            published = await published
        return published

    def _begin_publication(self, task_id: int, claim_token: str) -> str:
        with self.sessions() as session:
            task = session.execute(
                select(Task.publication_key, Task.publication_started_at).where(
                    Task.id == task_id,
                    Task.claim_token == claim_token,
                )
            ).one_or_none()
            if task is None:
                raise _ClaimLost
            publication_key = task.publication_key or secrets.token_urlsafe(32)
            started_at = task.publication_started_at or datetime.now(UTC)
            changed = cas_task_status(
                session,
                task_id,
                expected=_STATUS_EXPECTED["publishing"],
                new_status="publishing",
                claim_token=claim_token,
                actor="publisher",
                updates={
                    "publication_key": publication_key,
                    "publication_started_at": started_at,
                },
            )
            session.commit()
            if not changed:
                raise _ClaimLost
            return publication_key

    def _load_task(self, task_id: int) -> Task:
        with self.sessions() as session:
            task = session.scalar(
                select(Task)
                .options(
                    selectinload(Task.creator),
                    selectinload(Task.issue).selectinload(Issue.repository),
                )
                .where(Task.id == task_id)
            )
            if task is None:
                raise ValueError(f"task {task_id} does not exist")
            session.expunge(task)
            return task

    def _set_status(self, task_id: int, status: str, claim_token: str) -> None:
        expected = _STATUS_EXPECTED.get(status)
        if expected is None:
            raise ValueError(f"unsupported task transition target: {status}")
        with self.sessions() as session:
            if cas_task_status(
                session,
                task_id,
                expected=expected,
                new_status=status,
                claim_token=claim_token,
                actor="orchestrator",
            ):
                session.commit()
                return
            current = session.execute(
                select(Task.status, Task.claim_token).where(Task.id == task_id)
            ).one_or_none()
            if current is None:
                raise ValueError(f"task {task_id} does not exist")
            if current.claim_token != claim_token:
                raise _ClaimLost
            if current.status in {"cancelling", "cancelled", "dismissed"}:
                raise TaskCancelled
            raise RuntimeError(
                f"task {task_id} cannot transition from {current.status} to {status}"
            )

    def _set_prepared(
        self, task_id: int, claim_token: str, prepared: object
    ) -> None:
        with self.sessions() as session:
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "preparing",
                    Task.claim_token == claim_token,
                )
                .values(
                    workspace_path=str(prepared.workspace),
                    base_commit_sha=prepared.base_commit_sha,
                    branch_name=prepared.branch,
                )
            )
            session.commit()
            if result.rowcount != 1:
                raise _ClaimLost

    def _set_sealed(self, task_id: int, claim_token: str, sealed: object) -> None:
        with self.sessions() as session:
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "sealing",
                    Task.claim_token == claim_token,
                )
                .values(
                    fixed_patch_path=str(sealed.patch_path),
                    reviewed_tree_sha=sealed.tree_sha,
                )
            )
            session.commit()
            if result.rowcount != 1:
                raise _ClaimLost

    def _set_commit(self, task_id: int, claim_token: str, commit_sha: str) -> None:
        with self.sessions() as session:
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "sealing",
                    Task.claim_token == claim_token,
                )
                .values(commit_sha=commit_sha)
            )
            session.commit()
            if result.rowcount != 1:
                raise _ClaimLost

    async def _run_stage(
        self,
        task_id: int,
        status: str,
        stage: Stage,
        role: AgentRole,
        workspace: Path,
        prompt: str,
        claim_token: str,
    ):
        return await self.stage_executor.execute(
            task_id=task_id,
            status=status,
            stage=stage,
            role=role,
            workspace=workspace,
            prompt=prompt,
            claim_token=claim_token,
            cancel_event=self._cancel_events[task_id],
        )

    def _record_review(
        self, task_id: int, role: str, decision: str, findings: list[dict[str, Any]]
    ) -> None:
        with self.sessions() as session:
            session.add(
                Review(
                    task_id=task_id,
                    reviewer_role=role,
                    decision=decision,
                    findings=findings,
                    blocking_count=len(findings) if decision != "approve" else 0,
                    contract_version=REVIEWER_CONTRACT_VERSION,
                )
            )
            session.commit()

    def _awaiting_review(
        self, task_id: int, claim_token: str, published: object
    ) -> None:
        with self.sessions() as session:
            task = session.execute(
                select(Task.status, Task.publication_key).where(
                    Task.id == task_id,
                    Task.claim_token == claim_token,
                    Task.status.in_(("publishing", "cancelling")),
                )
            ).one_or_none()
            if task is None:
                raise _ClaimLost
            target = (
                "cancelled" if task.status == "cancelling" else "awaiting_human_review"
            )
            changed = cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status=target,
                claim_token=claim_token,
                actor="publisher",
                updates={
                    "pr_url": published.url,
                    "pr_number": published.number,
                    "pr_state": getattr(published, "state", "open"),
                    "claim_token": None,
                    "claim_expires_at": None,
                    "failure_code": None,
                    "failure_summary": None,
                    "finished_at": datetime.now(UTC),
                },
            )
            session.commit()
            if not changed:
                raise _ClaimLost

    def _fail(self, task_id: int, claim_token: str, exc: Exception) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None or task.claim_token != claim_token:
                return
            if cas_task_status(
                session,
                task_id,
                expected="cancelling",
                new_status="cancelled",
                claim_token=claim_token,
                actor="orchestrator",
                updates={
                    "claim_token": None,
                    "claim_expires_at": None,
                    "finished_at": datetime.now(UTC),
                },
            ):
                session.commit()
                return
            if task.status in {*TERMINAL_TASK_STATES, "cancelling"}:
                session.rollback()
                return
            changed = cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status="failed",
                claim_token=claim_token,
                actor="orchestrator",
                updates={
                    "claim_token": None,
                    "claim_expires_at": None,
                    "failure_code": type(exc).__name__,
                    "failure_summary": str(exc)[-2000:],
                    "finished_at": datetime.now(UTC),
                },
            )
            if changed and task.commit_sha is None:
                task.issue.triage_state = "discovered"
            session.commit()

    def _manual_intervention(
        self, task_id: int, claim_token: str, code: str, summary: str
    ) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if (
                task is None
                or task.claim_token != claim_token
                or task.status in {*TERMINAL_TASK_STATES, "cancelling"}
            ):
                return
            cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status="manual_intervention",
                claim_token=claim_token,
                actor="orchestrator",
                updates={
                    "claim_token": None,
                    "claim_expires_at": None,
                    "failure_code": code,
                    "failure_summary": summary,
                    "finished_at": datetime.now(UTC),
                },
            )
            session.commit()

    def _mark_cancelled(self, task_id: int, claim_token: str) -> None:
        with self.sessions() as session:
            if cas_task_status(
                session,
                task_id,
                expected="cancelling",
                new_status="cancelled",
                claim_token=claim_token,
                actor="orchestrator",
                updates={
                    "claim_token": None,
                    "claim_expires_at": None,
                    "finished_at": datetime.now(UTC),
                },
            ):
                session.commit()

    def _interrupt(self, task_id: int, claim_token: str) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None or task.claim_token != claim_token:
                return
            target = "cancelled" if task.status == "cancelling" else "manual_intervention"
            cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status=target,
                claim_token=claim_token,
                actor="orchestrator",
                updates={
                    "claim_token": None,
                    "claim_expires_at": None,
                    "failure_code": (
                        task.failure_code
                        if target == "cancelled"
                        else "worker_interrupted"
                    ),
                    "failure_summary": (
                        task.failure_summary
                        if target == "cancelled"
                        else "任务执行协程被中断"
                    ),
                    "finished_at": datetime.now(UTC),
                },
            )
            session.commit()

    async def _notify(self, task: Task, published: object) -> None:
        if self.notifier is None:
            return
        try:
            result = self.notifier.notify(
                database_task_id=task.id,
                task_id=f"RE-{task.id}",
                repository=f"{task.issue.repository.owner}/{task.issue.repository.name}",
                issue=f"#{task.issue.number} {task.issue.title}",
                creator=task.creator.username,
                pr_url=published.url,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            with self.sessions() as session:
                persisted = session.get(Task, task.id)
                persisted.failure_summary = f"飞书通知失败：{type(exc).__name__}"
                session.commit()


def deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings:
        key = json.dumps(finding, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def review_result(stdout: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        result = parse_reviewer_result(final_message(stdout).strip())
    except ReviewerResultError:
        return "changes_requested", [
            {"severity": "high", "message": "检视结果不符合版本化契约"}
        ]
    return result.decision, [finding.model_dump() for finding in result.findings]
