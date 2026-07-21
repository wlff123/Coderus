from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.integrations.feishu import FeishuClient, TaskCompletedMessage
from coderus.models import FeishuEvent


class FeishuTaskNotifier:
    def __init__(
        self,
        client: FeishuClient,
        *,
        session_factory: Callable[[], Session],
        default_chat_id: str | None,
    ) -> None:
        self._client = client
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
        message = TaskCompletedMessage(
            task_id=task_id,
            repository=repository,
            issue=issue,
            creator=creator,
            pr_url=pr_url,
        )
        await asyncio.to_thread(
            self._client.send_task_completed,
            message,
            receive_id=receive_id,
            receive_id_type="chat_id",
        )
