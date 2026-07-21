from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.models import Task, User
from coderus.tasks.quotas import can_start_task
from coderus.tasks.statuses import RUNNING_TASK_STATES


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
        with self.sessions() as session:
            tasks = session.scalars(
                select(Task).where(Task.status.in_(RUNNING_TASK_STATES))
            ).all()
            for task in tasks:
                task.status = "manual_intervention"
                task.failure_code = "manager_restarted"
                task.failure_summary = "服务重启中断了任务，需要人工检查后重试"
            session.commit()
            return len(tasks)

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)

    async def tick(self) -> None:
        self._running = {
            task_id: task for task_id, task in self._running.items() if not task.done()
        }
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
            task = asyncio.create_task(self.orchestrator.run(task_id))
            self._running[task_id] = task
            reserved_global += 1
            reserved_users[user_id] += 1

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
