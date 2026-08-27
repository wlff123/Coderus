"""PR 检视用例：统一 URL 校验、Forge 能力检查和入队事务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from coderus.forge import ForgeCapability, ForgeNotConfigured, ForgeRegistry
from coderus.forge.urls import parse_pull_request_url
from coderus.pr_review.service import enqueue_pr_review


@dataclass(frozen=True, slots=True)
class ReviewSource:
    chat_id: str
    message_id: str
    sender_id: str


class ReviewCommands:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        forges: ForgeRegistry,
    ) -> None:
        self._sessions = session_factory
        self._forges = forges

    def enqueue(self, url: str, source: ReviewSource) -> int:
        with self._sessions() as session:
            review_id = self.enqueue_in_session(session, url, source)
            session.commit()
            return review_id

    def enqueue_in_session(
        self, session: Session, url: str, source: ReviewSource
    ) -> int:
        source_repository, _ = parse_pull_request_url(url)
        if not self._forges.supports(
            source_repository.provider,
            ForgeCapability.GET_PULL_REQUEST,
            ForgeCapability.PUBLISH_PR_COMMENT,
        ):
            raise ForgeNotConfigured(source_repository.provider)
        task = enqueue_pr_review(
            session,
            url,
            source.chat_id,
            source.message_id,
            source.sender_id,
        )
        return task.id
