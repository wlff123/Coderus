"""任务操作用例：取消、关闭、PR 意见同步与再发布，事务边界在此收敛。"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from coderus.application.errors import Conflict, Forbidden, NotFound
from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.models import Issue, PRFeedback, Task, User
from coderus.tasks.statuses import RUNNING_TASK_STATES
from coderus.workflow.feedback import upsert_pr_feedback
from coderus.workflow.task_state import cas_task_status

CLOSABLE_TASK_STATUSES = frozenset(
    {"awaiting_human_review", "failed", "manual_intervention", "cancelled"}
)
TRUSTED_FEEDBACK_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


@dataclass(frozen=True, slots=True)
class CancelResult:
    should_signal_runner: bool


class TaskCommands:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        forges: ForgeRegistry,
    ) -> None:
        self._sessions = session_factory
        self._forges = forges

    def request_cancel(self, task_id: int, actor_id: int) -> CancelResult:
        with self._sessions() as session:
            task = self._owned_task(session, task_id, actor_id)
            if task.status == "queued":
                new_status = "cancelled"
                updates: dict[str, object] | None = {"finished_at": datetime.now(UTC)}
                should_signal = False
            elif task.status in RUNNING_TASK_STATES:
                new_status = "cancelling"
                updates = None
                should_signal = True
            else:
                raise Conflict("当前状态不能取消")
            self._transition(
                session, task_id, expected=task.status, new_status=new_status, updates=updates
            )
            session.commit()
            return CancelResult(should_signal_runner=should_signal)

    def close(self, task_id: int, actor_id: int) -> None:
        with self._sessions() as session:
            task = self._owned_task(session, task_id, actor_id)
            if task.status not in CLOSABLE_TASK_STATUSES:
                raise Conflict("当前状态不能关闭")
            self._transition(
                session,
                task_id,
                expected=task.status,
                new_status="dismissed",
                updates={"finished_at": datetime.now(UTC)},
            )
            session.commit()

    async def sync_feedback(self, task_id: int, actor_id: int) -> int:
        with self._sessions() as session:
            task = self._owned_task(
                session,
                task_id,
                actor_id,
                options=(selectinload(Task.issue).selectinload(Issue.repository),),
            )
            if task.status != "awaiting_human_review" or not task.pr_number:
                raise Conflict("当前任务不能同步 PR 意见")
            repository = task.issue.repository
            provider = repository.provider
            owner, name, pr_number = repository.owner, repository.name, task.pr_number
        if not self._forges.supports(provider, ForgeCapability.LIST_PR_FEEDBACK):
            raise Conflict("发布器不支持 PR 意见同步")
        forge = self._forges.get(provider)
        feedback = await forge.list_pr_feedback(owner, name, pr_number)
        pr_status = None
        if self._forges.supports(provider, ForgeCapability.GET_PR_STATUS):
            pr_status = await forge.get_pr_status(owner, name, pr_number)
        with self._sessions() as session:
            new_status = "awaiting_human_review"
            updates: dict[str, object] = {}
            if pr_status == "merged":
                new_status = "completed"
                updates = {"pr_state": "merged", "finished_at": datetime.now(UTC)}
            elif pr_status == "closed":
                new_status = "closed"
                updates = {"pr_state": "closed", "finished_at": datetime.now(UTC)}
            self._transition(
                session,
                task_id,
                expected="awaiting_human_review",
                new_status=new_status,
                updates=updates,
                conflict_message="任务状态已变化，未写入本次同步",
            )
            for item in feedback:
                upsert_pr_feedback(session, task_id=task_id, provider=provider, item=item)
            session.commit()
        return len(feedback)

    def queue_existing_publish(self, task_id: int, actor_id: int) -> None:
        with self._sessions() as session:
            task = self._owned_task(session, task_id, actor_id)
            if (
                not self._forges.supports(
                    task.issue.repository.provider, ForgeCapability.PUBLISH
                )
                or task.status not in {"manual_intervention", "failed"}
                or not task.commit_sha
                or not task.workspace_path
                or not task.branch_name
                or not Path(task.workspace_path).is_dir()
            ):
                raise Conflict("当前任务不能按现状发布")
            self._transition(
                session,
                task_id,
                expected=task.status,
                new_status="queued",
                updates={
                    "failure_code": "publish_existing",
                    "failure_summary": None,
                    "finished_at": None,
                },
            )
            session.commit()

    def queue_feedback_revision(
        self, task_id: int, actor_id: int, feedback_ids: Collection[int]
    ) -> None:
        with self._sessions() as session:
            task = self._owned_task(session, task_id, actor_id)
            if task.status != "awaiting_human_review" or not feedback_ids:
                raise Conflict("当前任务不能处理 PR 意见")
            selected_ids = set(feedback_ids)
            rows = session.scalars(
                select(PRFeedback).where(
                    PRFeedback.task_id == task_id,
                    PRFeedback.id.in_(selected_ids),
                    PRFeedback.processed_at.is_(None),
                    PRFeedback.author_association.in_(TRUSTED_FEEDBACK_ASSOCIATIONS),
                )
            ).all()
            if len(rows) != len(selected_ids):
                raise Conflict("只能处理可信维护者的未处理意见")
            now = datetime.now(UTC)
            self._transition(
                session,
                task_id,
                expected="awaiting_human_review",
                new_status="queued",
                updates={
                    "failure_code": "pr_feedback_revision",
                    "failure_summary": None,
                    "finished_at": None,
                },
            )
            for row in rows:
                row.selected_at = now
            session.commit()

    @staticmethod
    def _owned_task(
        session: Session,
        task_id: int,
        actor_id: int,
        *,
        options: tuple[object, ...] = (),
    ) -> Task:
        statement = select(Task).where(Task.id == task_id)
        if options:
            statement = statement.options(*options)
        task = session.scalar(statement)
        if task is None:
            raise NotFound("任务不存在")
        actor = session.get(User, actor_id)
        if actor is None or (actor.role != "admin" and task.created_by != actor.id):
            raise Forbidden("没有权限操作该任务")
        return task

    @staticmethod
    def _transition(
        session: Session,
        task_id: int,
        *,
        expected: str,
        new_status: str,
        updates: dict[str, object] | None,
        conflict_message: str = "任务状态已变化，请刷新后重试",
    ) -> None:
        if not cas_task_status(
            session,
            task_id,
            expected=expected,
            new_status=new_status,
            updates=updates,
        ):
            session.rollback()
            raise Conflict(conflict_message)
