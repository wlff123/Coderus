from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.models import FeishuEvent


class FeishuTaskNotifier:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        default_chat_id: str | None,
    ) -> None:
        self._sessions = session_factory
        self._default_chat_id = default_chat_id

    async def notify(
        self,
        *,
        database_task_id: int,
        task_id: str,
        repository: str,
        issue: str,
        creator: str,
        pr_url: str,
    ) -> None:
        with self._sessions() as session:
            origin_chat_id = session.scalar(
                select(FeishuEvent.chat_id)
                .where(FeishuEvent.task_id == database_task_id)
                .order_by(FeishuEvent.id)
                .limit(1)
            )
        receive_id = origin_chat_id or self._default_chat_id
        if receive_id is None:
            return
        text = "\n".join(
            (
                "Coderus 任务已完成",
                f"任务：{task_id}",
                f"仓库：{repository}",
                f"Issue：{issue}",
                f"创建人：{creator}",
                f"PR：{pr_url}",
            )
        )
        message_id = f"task-completed:{database_task_id}"
        with self._sessions() as session:
            existing = session.scalar(
                select(FeishuEvent).where(FeishuEvent.message_id == message_id)
            )
            if existing is None:
                session.add(
                    FeishuEvent(
                        message_id=message_id,
                        event_id=message_id,
                        chat_id=receive_id,
                        chat_type="system",
                        sender_open_id="<system>",
                        command="task_completed",
                        status="processed",
                        task_id=database_task_id,
                        reply_text=text,
                        reply_status="pending",
                    )
                )
                session.commit()
