"""任务租约：CAS 续约与心跳保活，Issue 工作流与 PR 检视编排器共用。

任务模型只需提供 id、status、claim_token、claim_expires_at 四列。
心跳续约在独立线程执行：事件循环被重 I/O（如大仓库克隆落盘）阻塞时，
租约依然按时续期，不会被误判为持有者失联。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
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
    stop: threading.Event,
    on_lost: Callable[[], None],
    *,
    interval: float = CLAIM_HEARTBEAT_SECONDS,
    lease_seconds: float = CLAIM_LEASE_SECONDS,
    log_label: str = "task",
) -> None:
    """周期续约直到 stop 置位；租约确认丢失时调用 on_lost 并提前返回。

    续约在专用线程执行，免疫事件循环阻塞；on_lost 会被调度回事件循环
    线程执行。CAS 续约返回 False 说明租约确已易主或过期，立即报告丢失；
    续约抛异常视为暂时故障（如数据库繁忙），只要距上次成功续约仍在
    租约窗口内就继续重试。
    """
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def signal(callback: Callable[[], None]) -> None:
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass  # 事件循环已关闭，通知无处送达也无人消费

    def worker() -> None:
        last_renewed = time.monotonic()
        try:
            while not stop.wait(interval):
                try:
                    renewed = renew()
                except Exception as exc:
                    logger.error(
                        "%s lease heartbeat failed: %s", log_label, type(exc).__name__
                    )
                    if time.monotonic() - last_renewed < lease_seconds:
                        continue
                    renewed = False
                if not renewed:
                    if not stop.is_set():
                        signal(on_lost)
                    return
                last_renewed = time.monotonic()
        finally:
            signal(finished.set)

    thread = threading.Thread(
        target=worker, name=f"lease-heartbeat-{log_label}", daemon=True
    )
    thread.start()
    try:
        await finished.wait()
    finally:
        stop.set()
        await asyncio.to_thread(thread.join)
