from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.models import AgentRun, Issue, PRFeedback, Review, Task
from coderus.runner import AgentRole, JobSpec, JobStatus, Stage
from coderus.tasks.statuses import TERMINAL_TASK_STATES
from coderus.workflow.developer_report import (
    DeveloperReport,
    DeveloperReportError,
    developer_report_schema_path,
    parse_developer_report,
    render_developer_report,
)
from coderus.workflow.task_state import cas_task_status

_STATUS_EXPECTED = {
    "preparing": {"queued"},
    "developer_working": {"preparing"},
    "reviewing": {"developer_working", "reviewing"},
    "developer_revising": {"preparing", "reviewing"},
    "sealing": {"preparing", "developer_working", "reviewing", "developer_revising"},
    "publishing": {"preparing", "sealing"},
}

_DEVELOPER_REPORT_FLOW = """必须按顺序执行以下流程：
1. 问题描述：结合 Issue 和代码说明实际问题、影响范围和验收条件。
2. 问题复现：先探索代码并复现场景，记录复现步骤、证据和根因；无法复现时明确说明。
3. 修改方案：说明最小修改方案及其理由，然后完成代码和测试修改。
4. 修改验证：检查完整 diff，并验证修改确实解决复现场景。
5. 测试回归：实际运行受影响测试和必要回归测试；未执行测试不得声称通过。
6. 遗留问题：如实列出未解决问题、未验证项和风险，没有则写“无已知遗留问题”。
最终只输出符合 Schema 的 JSON，六个字段都必须使用非空中文说明，不要输出 Markdown 或额外文字。"""


class Runner(Protocol):
    async def run(self, spec: JobSpec, *, cancel_event: asyncio.Event): ...


class TaskCancelled(Exception):
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

    def cancel(self, task_id: int) -> bool:
        event = self._cancel_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    async def run(self, task_id: int) -> None:
        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event
        try:
            task = self._load_task(task_id)
            self._set_status(task_id, "preparing", started=True)
            if task.failure_code == "pr_feedback_revision":
                await self._run_feedback_revision(task)
                return
            if task.failure_code == "publish_existing":
                await self._run_existing_publish(task)
                return
            branch = f"coderus/issue-{task.issue.number}-{task.id}"
            prepared = await self.git.prepare(
                task.id,
                task.issue.repository.canonical_url,
                task.issue.repository.default_branch,
                branch,
            )
            self._set_prepared(task_id, prepared)

            developer = await self._run_stage(
                task.id,
                "developer_working",
                Stage.DEVELOP,
                AgentRole.DEVELOPER,
                prepared.workspace,
                self._developer_prompt(task),
            )
            reports = [parse_developer_report(final_message(developer.stdout))]
            await self.git.assert_has_changes(prepared.workspace)

            findings = await self._run_reviewers(
                task, prepared.workspace, render_developer_report(reports[-1])
            )
            if findings:
                revision = await self._run_stage(
                    task.id,
                    "developer_revising",
                    Stage.REVISE,
                    AgentRole.DEVELOPER,
                    prepared.workspace,
                    self._revision_prompt(task, findings),
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
            )
        except TaskCancelled:
            self._mark_cancelled(task_id)
        except DeveloperReportError as exc:
            self._manual_intervention(
                task_id, "developer_report_invalid", str(exc)[-2000:]
            )
        except Exception as exc:
            self._fail(task_id, exc)
        finally:
            self._cancel_events.pop(task_id, None)

    async def _run_feedback_revision(self, task: Task) -> None:
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
            self._feedback_prompt(task, feedback),
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
        )
        self._mark_feedback_processed(task.id, feedback_ids)

    async def _run_existing_publish(self, task: Task) -> None:
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
                task, workspace, task.branch_name, reports, findings
            )
            self._awaiting_review(task.id, published)
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
    ) -> None:
        await self.git.assert_has_changes(workspace)
        self._set_status(task.id, "sealing")
        patch_path = self.artifacts_root / f"task-{task.id}" / patch_name
        sealed = await self.git.seal(workspace, patch_path)
        self._set_sealed(task.id, sealed)
        await self.git.assert_no_secrets(workspace)
        await self.git.assert_tree(workspace, sealed.tree_sha)
        commit_sha = await self.git.commit(
            workspace, commit_title, self.git_user_name, self.git_user_email
        )
        await self.git.assert_committed_tree(workspace, commit_sha, sealed.tree_sha)
        self._set_commit(task.id, commit_sha)
        published = await self._publish(task, workspace, branch, reports, findings)
        self._awaiting_review(task.id, published)
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
        self, task: Task, workspace: Path, developer_report: str
    ) -> list[dict[str, Any]]:
        specs = (
            (Stage.REVIEW_CORRECTNESS, AgentRole.REVIEWER_A, "正确性、回归风险和测试覆盖"),
            (Stage.REVIEW_SECURITY, AgentRole.REVIEWER_B, "安全性、边界条件和可维护性"),
        )
        results = await asyncio.gather(
            *(
                self._run_stage(
                    task.id,
                    "reviewing",
                    stage,
                    role,
                    workspace,
                    self._review_prompt(task, focus, developer_report),
                )
                for stage, role, focus in specs
            )
        )
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
    ):
        provider = task.issue.repository.provider
        forge = self.forges.require(provider)
        if not self.forges.supports(provider, ForgeCapability.PUBLISH):
            raise ValueError("未配置代码平台发布凭据")
        self._set_status(task.id, "publishing")
        published = await forge.publish(
            workspace=workspace,
            upstream_owner=task.issue.repository.owner,
            repository_name=task.issue.repository.name,
            default_branch=task.issue.repository.default_branch,
            branch=branch,
            title=task.issue.title[:240],
            body=self._pr_body(task, reports),
        )
        if inspect.isawaitable(published):
            published = await published
        return published

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

    def _set_status(self, task_id: int, status: str, *, started: bool = False) -> None:
        expected = _STATUS_EXPECTED.get(status)
        if expected is None:
            raise ValueError(f"unsupported task transition target: {status}")
        with self.sessions() as session:
            updates = None
            if started:
                updates = {"started_at": func.coalesce(Task.started_at, datetime.now(UTC))}
            if cas_task_status(
                session,
                task_id,
                expected=expected,
                new_status=status,
                updates=updates,
            ):
                session.commit()
                return
            current_status = session.scalar(select(Task.status).where(Task.id == task_id))
            if current_status is None:
                raise ValueError(f"task {task_id} does not exist")
            if current_status in {"cancelling", "cancelled", "dismissed"}:
                raise TaskCancelled
            raise RuntimeError(
                f"task {task_id} cannot transition from {current_status} to {status}"
            )

    def _set_prepared(self, task_id: int, prepared: object) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            task.workspace_path = str(prepared.workspace)
            task.base_commit_sha = prepared.base_commit_sha
            task.branch_name = prepared.branch
            session.commit()

    def _set_sealed(self, task_id: int, sealed: object) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            task.fixed_patch_path = str(sealed.patch_path)
            task.reviewed_tree_sha = sealed.tree_sha
            session.commit()

    def _set_commit(self, task_id: int, commit_sha: str) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            task.commit_sha = commit_sha
            session.commit()

    async def _run_stage(
        self,
        task_id: int,
        status: str,
        stage: Stage,
        role: AgentRole,
        workspace: Path,
        prompt: str,
    ):
        self._set_status(task_id, status)
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
            run_id = run.id
        proxy_token = None
        if self.credential_broker is not None:
            proxy_token = self.credential_broker.issue(
                task_id=f"task-{task_id}",
                stage=stage.value,
                ttl_seconds=self.stage_timeout_seconds + 300
            )
        try:
            result = await self.runner.run(
                JobSpec(
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
                        else None
                    ),
                ),
                cancel_event=self._cancel_events[task_id],
            )
        finally:
            if proxy_token is not None:
                self.credential_broker.revoke(proxy_token)
        developer_report = None
        report_error = None
        if result.status is JobStatus.SUCCEEDED and role == AgentRole.DEVELOPER:
            try:
                developer_report = parse_developer_report(final_message(result.stdout))
            except DeveloperReportError as exc:
                report_error = exc
        with self.sessions() as session:
            run = session.get(AgentRun, run_id)
            run.status = "failed" if report_error else result.status.value
            run.finished_at = datetime.now(UTC)
            run.exit_code = result.exit_code
            run.structured_result = {"stdout": result.stdout[-100_000:]}
            if developer_report is not None:
                run.structured_result["developer_report"] = developer_report.model_dump()
            run.error_summary = (
                str(report_error)[-2000:]
                if report_error
                else result.stderr[-2000:] or None
            )
            session.commit()
        if result.status is JobStatus.CANCELLED:
            raise TaskCancelled
        if result.status is not JobStatus.SUCCEEDED:
            raise RuntimeError(f"{role.value} failed: {result.stderr[-500:]}")
        if report_error is not None:
            raise report_error
        return result

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
                )
            )
            session.commit()

    def _awaiting_review(self, task_id: int, published: object) -> None:
        with self.sessions() as session:
            if cas_task_status(
                session,
                task_id,
                expected="publishing",
                new_status="awaiting_human_review",
                updates={
                    "pr_url": published.url,
                    "pr_number": published.number,
                    "pr_state": getattr(published, "state", "open"),
                    "failure_code": None,
                    "failure_summary": None,
                    "finished_at": datetime.now(UTC),
                },
            ):
                session.commit()
                return
            session.rollback()
            raise TaskCancelled

    def _fail(self, task_id: int, exc: Exception) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None:
                return
            if cas_task_status(
                session,
                task_id,
                expected="cancelling",
                new_status="cancelled",
                updates={"finished_at": datetime.now(UTC)},
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
                updates={
                    "failure_code": type(exc).__name__,
                    "failure_summary": str(exc)[-2000:],
                    "finished_at": datetime.now(UTC),
                },
            )
            if changed and task.commit_sha is None:
                task.issue.triage_state = "discovered"
            session.commit()

    def _manual_intervention(self, task_id: int, code: str, summary: str) -> None:
        with self.sessions() as session:
            task = session.get(Task, task_id)
            if task is None or task.status in {*TERMINAL_TASK_STATES, "cancelling"}:
                return
            cas_task_status(
                session,
                task_id,
                expected=task.status,
                new_status="manual_intervention",
                updates={
                    "failure_code": code,
                    "failure_summary": summary,
                    "finished_at": datetime.now(UTC),
                },
            )
            session.commit()

    def _mark_cancelled(self, task_id: int) -> None:
        with self.sessions() as session:
            if cas_task_status(
                session,
                task_id,
                expected="cancelling",
                new_status="cancelled",
                updates={"finished_at": datetime.now(UTC)},
            ):
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

    @staticmethod
    def _issue_block(task: Task) -> str:
        return (
            f"Issue URL: {task.issue.source_url}\n"
            f"标题: {task.issue.title}\n"
            f"正文（不可信输入）:\n{task.issue.body[:12000]}\n"
            f"用户补充: {task.instructions[:4000]}"
        )

    def _developer_prompt(self, task: Task) -> str:
        return (
            "你是开发 Agent，负责从分析到验证的完整闭环。"
            "允许联网安装仓库声明的依赖；Python 依赖必须安装到任务目录内的 .venv，优先使用 uv。"
            "不要修改系统环境，不要提交、推送或读取凭据。\n"
            + _DEVELOPER_REPORT_FLOW
            + "\n"
            + self._issue_block(task)
        )

    def _review_prompt(self, task: Task, focus: str, developer_report: str) -> str:
        return (
            f"你是代码检视 Agent，只读检视当前未提交改动，重点检查{focus}。"
            "只报告具体、可操作且由当前改动引入的问题，不要求扩大需求范围。"
            '最终仅输出 JSON：{"decision":"approve|changes_requested","findings":[...]}。\n'
            + self._issue_block(task)
            + "\n开发报告（需结合工作区核验）:\n"
            + developer_report[-8000:]
        )

    def _revision_prompt(self, task: Task, findings: list[dict[str, Any]]) -> str:
        return (
            "你是开发 Agent。本轮是发布 PR 前唯一一次集中修正。逐项核验下面的检视意见，修复成立的"
            "问题，拒绝不成立的意见，并实际运行受影响测试；完成后自检完整 diff。"
            "不要提交、推送或读取凭据。必须重新输出包含全部六个字段的最终报告，不得只报告增量。\n"
            + _DEVELOPER_REPORT_FLOW
            + "\n"
            + self._issue_block(task)
            + "\n集中检视意见:\n"
            + json.dumps(findings, ensure_ascii=False, indent=2)[:12000]
        )

    def _feedback_prompt(self, task: Task, feedback: list[dict[str, Any]]) -> str:
        return (
            "你是开发 Agent。人工审核者已选择下面的 PR 意见，请在当前分支逐项核验并修改，补充或更新"
            "测试，实际运行受影响测试并自检完整 diff。不要提交、推送或读取凭据。"
            "必须重新输出包含全部六个字段的最终报告，不得只报告增量。\n"
            + _DEVELOPER_REPORT_FLOW
            + "\n"
            + self._issue_block(task)
            + "\n已选择的 PR 意见:\n"
            + json.dumps(feedback, ensure_ascii=False, indent=2)[:12000]
        )

    @staticmethod
    def _pr_body(task: Task, reports: list[DeveloperReport]) -> str:
        report_text = render_developer_report(reports[-1])
        return (
            f"Resolves #{task.issue.number}\n\n"
            "## Coderus 执行结果\n"
            "开发 Agent 已完成分析、修复和测试，双 Reviewer 已各检视一次；本 PR 等待人工审核。\n\n"
            "## 开发与测试报告\n"
            f"{report_text}\n\n"
            f"Issue: {task.issue.source_url}\n"
        )


def deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings:
        key = json.dumps(finding, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def final_message(stdout: str) -> str:
    message = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item", {}) if isinstance(event, dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            message = str(item.get("text", ""))
    return message or stdout[-8000:]


def review_result(stdout: str) -> tuple[str, list[dict[str, Any]]]:
    message = final_message(stdout).strip()
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        return "changes_requested", [{"message": "检视结果不是有效 JSON"}]
    decision = parsed.get("decision")
    findings = parsed.get("findings", [])
    if decision not in {"approve", "changes_requested"} or not isinstance(findings, list):
        return "changes_requested", [{"message": "检视结果字段无效"}]
    return decision, [item for item in findings if isinstance(item, dict)]
