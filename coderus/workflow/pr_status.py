from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.models import Issue, Repository, Task
from coderus.workflow.task_state import cas_task_status


class PRStatusPoller:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        forges: ForgeRegistry,
        poll_seconds: float,
        can_run: Callable[[], bool] | None = None,
    ) -> None:
        self.sessions = session_factory
        self.forges = forges
        self.poll_seconds = poll_seconds
        self.can_run = can_run or (lambda: True)
        self._loop_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    async def tick(self) -> None:
        if not self.can_run():
            return
        with self.sessions() as session:
            pulls = session.execute(
                select(
                    Task.id,
                    Repository.provider,
                    Repository.owner,
                    Repository.name,
                    Task.pr_number,
                )
                .join(Task.issue)
                .join(Issue.repository)
                .where(
                    Task.status == "awaiting_human_review",
                    Task.pr_number.is_not(None),
                )
            ).all()
        for task_id, provider, owner, name, number in pulls:
            if not self.forges.supports(provider, ForgeCapability.GET_PR_STATUS):
                continue
            forge = self.forges.require(provider)
            try:
                status = await forge.get_pr_status(owner, name, number)
            except Exception:
                continue
            if status not in {"merged", "closed"}:
                continue
            with self.sessions() as session:
                if cas_task_status(
                    session,
                    task_id,
                    expected="awaiting_human_review",
                    new_status="completed" if status == "merged" else "closed",
                    updates={
                        "pr_state": status,
                        "finished_at": datetime.now(UTC),
                    },
                ):
                    session.commit()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
