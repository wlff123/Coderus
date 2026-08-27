"""Issue 派发用例：网页、飞书和未来 API 共用同一事务与校验行为。"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sqlalchemy.orm import Session

from coderus.issues.service import (
    IssueProvider,
    add_and_dispatch_issue,
    add_provider_issue,
    dispatch_issue,
)
from coderus.models import Issue, User


def _active_user(session: Session, actor_id: int) -> User:
    actor = session.get(User, actor_id)
    if actor is None or not actor.is_active:
        raise ValueError("用户不存在或已停用")
    return actor


class IssueCommands:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        providers: Mapping[str, IssueProvider],
    ) -> None:
        self._sessions = session_factory
        self._providers = providers

    def add_issue(self, issue_url: str) -> int:
        """按 URL 添加单个 Issue，返回 Issue 编号。"""
        with self._sessions() as session:
            issue = add_provider_issue(session, self._providers, issue_url)
            number = issue.number
            session.commit()
            return number

    def dispatch(self, issue_id: int, actor_id: int, instructions: str = "") -> int:
        with self._sessions() as session:
            task_id = self.dispatch_in_session(
                session, issue_id, actor_id, instructions
            )
            session.commit()
            return task_id

    def dispatch_in_session(
        self,
        session: Session,
        issue_id: int,
        actor_id: int,
        instructions: str = "",
    ) -> int:
        actor = _active_user(session, actor_id)
        issue = session.get(Issue, issue_id)
        if issue is None:
            raise ValueError("Issue 不存在")
        task = dispatch_issue(session, issue, actor, instructions, commit=False)
        return task.id

    def add_and_dispatch(self, issue_url: str, actor_id: int) -> int:
        with self._sessions() as session:
            task_id = self.add_and_dispatch_in_session(session, issue_url, actor_id)
            session.commit()
            return task_id

    def add_and_dispatch_in_session(
        self, session: Session, issue_url: str, actor_id: int
    ) -> int:
        actor = _active_user(session, actor_id)
        task = add_and_dispatch_issue(
            session, self._providers, issue_url, actor, commit=False
        )
        return task.id
