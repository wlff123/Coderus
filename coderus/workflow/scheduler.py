from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from coderus.models import AgentRun, Task, User
from coderus.tasks.quotas import can_start_task
from coderus.tasks.statuses import RUNNING_TASK_STATES
from coderus.workflow.task_state import cas_task_status, claim_queued_task

CLAIM_LEASE_SECONDS = 120.0


class TaskScheduler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        orchestrator: object,
        global_limit: int,
        per_user_limit: int,
        poll_seconds: float,
        can_claim: Callable[[], bool] | None = None,
    ) -> None:
        self.sessions = session_factory
        self.orchestrator = orchestrator
        self.global_limit = global_limit
        self.per_user_limit = per_user_limit
        self.poll_seconds = poll_seconds
        self.can_claim = can_claim or (lambda: True)
        self._running: dict[int, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._loop_task is None:
            self.recover_interrupted()
            self._loop_task = asyncio.create_task(self._loop())

    def recover_interrupted(self) -> int:
        return self._recover_expired()

    def _recover_expired(self) -> int:
        now = datetime.now(UTC)
        with self.sessions() as session:
            tasks = session.scalars(
                select(Task).where(
                    or_(
                        Task.status == "cancelling",
                        and_(
                            Task.status.in_(RUNNING_TASK_STATES),
                            or_(
                                Task.claim_expires_at.is_(None),
                                Task.claim_expires_at <= now,
                            ),
                        ),
                    ),
                )
            ).all()
            recovered = 0
            for task in tasks:
                reconcile_publication = (
                    task.status == "publishing"
                    and bool(task.workspace_path)
                    and bool(task.branch_name)
                    and bool(task.commit_sha)
                )
                target = (
                    "cancelled"
                    if task.status == "cancelling"
                    else "queued"
                    if reconcile_publication
                    else "manual_intervention"
                )
                changed = cas_task_status(
                    session,
                    task.id,
                    expected=task.status,
                    new_status=target,
                    claim_token=task.claim_token,
                    actor="recovery",
                    updates={
                        "claim_token": None,
                        "claim_expires_at": None,
                        "failure_code": (
                            task.failure_code
                            if target == "cancelled"
                            else "publish_existing"
                            if reconcile_publication
                            else "manager_restarted"
                        ),
                        "failure_summary": (
                            task.failure_summary
                            if target == "cancelled"
                            else "发布过程被中断，系统将按固定分支核对并恢复 PR 记录"
                            if reconcile_publication
                            else "服务重启或任务租约过期，运行已中断，请人工检查后重试"
                        ),
                        "finished_at": None if reconcile_publication else now,
                    },
                )
                if not changed:
                    continue
                session.execute(
                    update(AgentRun)
                    .where(AgentRun.task_id == task.id, AgentRun.status == "running")
                    .values(
                        status="interrupted",
                        finished_at=now,
                        error_summary="任务租约过期或服务重启",
                    )
                )
                recovered += 1
            session.commit()
            return recovered

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._running:
            workers = tuple(self._running.values())
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._running.clear()

    async def tick(self) -> None:
        self._running = {
            task_id: task for task_id, task in self._running.items() if not task.done()
        }
        self._recover_expired()
        if not self.can_claim():
            return
        with self.sessions() as session:
            persisted = session.execute(
                select(Task.created_by, Task.status).where(
                    Task.status.in_(RUNNING_TASK_STATES)
                )
            ).all()
            queued = session.execute(
                select(Task.id, Task.created_by)
                .join(User, Task.created_by == User.id)
                .where(Task.status == "queued")
                .where(User.is_active.is_(True))
                .order_by(Task.priority.desc(), Task.created_at, Task.id)
            ).all()

        running_users = Counter(user_id for user_id, _ in persisted)
        running_global = len(persisted)
        reserved_users: Counter[int] = Counter()
        reserved_global = 0
        for task_id, user_id in queued:
            if task_id in self._running:
                continue
            if not can_start_task(
                global_running=running_global + reserved_global,
                user_running=running_users[user_id] + reserved_users[user_id],
                global_limit=self.global_limit,
                user_limit=self.per_user_limit,
            ):
                continue
            with self.sessions() as session:
                claim_token = claim_queued_task(
                    session,
                    task_id,
                    global_limit=self.global_limit,
                    per_user_limit=self.per_user_limit,
                    lease_seconds=CLAIM_LEASE_SECONDS,
                )
                session.commit()
            if claim_token is None:
                continue
            worker = asyncio.create_task(
                self.orchestrator.run(task_id, claim_token=claim_token)
            )
            self._running[task_id] = worker
            reserved_global += 1
            reserved_users[user_id] += 1

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
