from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.issues.service import sync_repository
from coderus.models import Repository

logger = logging.getLogger(__name__)


class IssuePoller:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        providers: Mapping[str, object],
        poll_seconds: float,
        can_run: Callable[[], bool] | None = None,
        full_sync_every: int = 24,
    ) -> None:
        if full_sync_every < 1:
            raise ValueError("full_sync_every must be positive")
        self.sessions = session_factory
        self.providers = providers
        self.poll_seconds = poll_seconds
        self.can_run = can_run or (lambda: True)
        self.full_sync_every = full_sync_every
        self._successful_syncs: dict[int, int] = {}
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
            repository_ids = session.scalars(
                select(Repository.id).where(
                    Repository.is_enabled.is_(True),
                    Repository.sync_status != "running",
                )
            ).all()
        for repository_id in repository_ids:
            count = self._successful_syncs.get(repository_id, 0)
            full = count % self.full_sync_every == 0
            succeeded = await asyncio.to_thread(self._sync_one, repository_id, full)
            if succeeded:
                self._successful_syncs[repository_id] = count + 1

    def _sync_one(self, repository_id: int, full: bool) -> bool:
        with self.sessions() as session:
            repository = session.get(Repository, repository_id)
            if repository is None or not repository.is_enabled:
                return False
            try:
                sync_repository(
                    session,
                    repository,
                    self.providers[repository.provider],
                    full=full,
                )
                session.commit()
            except Exception as exc:
                repository.sync_status = "failed"
                repository.last_sync_error = str(exc)[:1000]
                session.commit()
                logger.error(
                    "issue_sync_failed",
                    extra={
                        "provider": repository.provider,
                        "repository_id": repository.id,
                        "error_type": type(exc).__name__,
                    },
                )
                return False
            return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
