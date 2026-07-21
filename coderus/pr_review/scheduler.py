from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from coderus.models import PRReviewTask

RUNNING_STATUSES = ("preparing", "reviewing", "commenting")
logger = logging.getLogger(__name__)


class PRReviewScheduler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        orchestrator: object,
        poll_seconds: float,
        can_claim: Callable[[], bool] | None = None,
    ) -> None:
        self.sessions = session_factory
        self.orchestrator = orchestrator
        self.poll_seconds = poll_seconds
        self.can_claim = can_claim or (lambda: True)
        self._running: dict[int, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._loop_task is not None:
            return
        self._stop.clear()
        self.recover_expired()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            loop_results = await asyncio.gather(
                self._loop_task, return_exceptions=True
            )
            self._observe_loop_result(loop_results[0])
            self._loop_task = None
        if self._running:
            running = tuple(self._running.items())
            for _, task in running:
                task.cancel()
            results = await asyncio.gather(
                *(task for _, task in running), return_exceptions=True
            )
            for (task_id, _), result in zip(running, results, strict=True):
                self._observe_worker_result(task_id, result)
            self._running.clear()

    async def tick(self) -> None:
        if not self.can_claim():
            return
        self.recover_expired()
        for task_id, task in tuple(self._running.items()):
            if not task.done():
                continue
            result = (
                asyncio.CancelledError()
                if task.cancelled()
                else task.exception()
            )
            self._observe_worker_result(task_id, result)
            del self._running[task_id]
        if self._running:
            return

        with self.sessions() as session:
            task_id = session.scalar(
                select(PRReviewTask.id)
                .where(PRReviewTask.status == "queued")
                .order_by(PRReviewTask.created_at, PRReviewTask.id)
                .limit(1)
            )
        if task_id is not None:
            self._running[task_id] = asyncio.create_task(
                self.orchestrator.run(task_id)
            )

    def recover_expired(self) -> int:
        now = datetime.now(UTC)
        with self.sessions() as session:
            result = session.execute(
                update(PRReviewTask)
                .where(
                    PRReviewTask.status.in_(RUNNING_STATUSES),
                    or_(
                        PRReviewTask.claim_expires_at.is_(None),
                        PRReviewTask.claim_expires_at <= now,
                    ),
                )
                .values(
                    status="queued",
                    claim_token=None,
                    claim_expires_at=None,
                    started_at=None,
                    finished_at=None,
                    failure_code=None,
                    failure_summary=None,
                )
            )
            session.commit()
            return result.rowcount

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_seconds
                )
            except TimeoutError:
                pass

    def _observe_worker_result(
        self, task_id: int, result: object
    ) -> None:
        if not isinstance(result, BaseException) or isinstance(
            result, asyncio.CancelledError
        ):
            return
        kind = type(result).__name__
        logger.error("PR review worker %s failed: %s", task_id, kind)

    @staticmethod
    def _observe_loop_result(result: object) -> None:
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError
        ):
            logger.error("PR review scheduler loop failed: %s", type(result).__name__)
