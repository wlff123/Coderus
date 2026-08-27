"""任务租约：CAS 续约与心跳保活，Issue 工作流与 PR 检视编排器共用。

任务模型只需提供 id、status、claim_token、claim_expires_at 四列。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

logger = logging.getLogger(__name__)

CLAIM_LEASE_SECONDS = 120.0
CLAIM_HEARTBEAT_SECONDS = 10.0


class TaskLease:
    """对单一任务模型执行租约续期；持有者、状态或时限不符时续期失败。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        model: type,
        active_statuses: Collection[str],
        lease_seconds: float = CLAIM_LEASE_SECONDS,
    ) -> None:
        self._sessions = session_factory
        self._model = model
        self._active_statuses = tuple(active_statuses)
        self._lease_seconds = lease_seconds

    def renew(self, task_id: int, claim_token: str) -> bool:
        model = self._model
        now = datetime.now(UTC)
        with self._sessions() as session:
            result = session.execute(
                update(model)
                .where(
                    model.id == task_id,
                    model.status.in_(self._active_statuses),
                    model.claim_token == claim_token,
                    model.claim_expires_at > now,
                )
                .values(
                    claim_expires_at=now + timedelta(seconds=self._lease_seconds)
                )
            )
            session.commit()
            return result.rowcount == 1


async def heartbeat_loop(
    renew: Callable[[], bool],
    stop: asyncio.Event,
    on_lost: Callable[[], None],
    *,
    interval: float = CLAIM_HEARTBEAT_SECONDS,
    log_label: str = "task",
) -> None:
    """周期续约直到 stop 置位；续约失败或异常时调用 on_lost 并退出。"""
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            try:
                renewed = renew()
            except Exception as exc:
                logger.error(
                    "%s lease heartbeat failed: %s", log_label, type(exc).__name__
                )
                on_lost()
                return
            if not renewed:
                on_lost()
                return
