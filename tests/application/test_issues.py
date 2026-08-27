from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from coderus.application.errors import Conflict, Forbidden, NotFound
from coderus.application.issues import IssueCommands
from coderus.models import Issue, Task
from coderus.providers.models import Issue as ProviderIssue
from coderus.providers.models import Repository as ProviderRepository

from .conftest import seed_issue, seed_task, seed_user


class FakeGitHubProvider:
    name = "github"

    def get_issue(self, repository: ProviderRepository, number: int) -> ProviderIssue:
        return ProviderIssue(
            repository=repository,
            external_id=str(number),
            number=number,
            title="Fix failing test",
            body="Reproduction details",
            state="open",
            labels=("bug",),
            canonical_url=f"{repository.canonical_url}/issues/{number}",
            created_at=None,
            updated_at=datetime(2026, 7, 15, tzinfo=UTC),
        )


def commands(session_factory) -> IssueCommands:
    return IssueCommands(
        session_factory=session_factory,
        providers={"github": FakeGitHubProvider()},
    )


def test_dispatch_persists_task_with_instructions(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        issue_id, admin_id = issue.id, issue.repository.created_by

    task_id = commands(session_factory).dispatch(issue_id, admin_id, "优先验证回归")

    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.instructions == "优先验证回归"
        assert task.issue.triage_state == "dispatched"
        assert task.created_by == admin_id


def test_dispatch_rejects_inactive_or_unknown_actor(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        inactive = seed_user(session, username="inactive", role="user", is_active=False)
        issue_id, inactive_id = issue.id, inactive.id

    with pytest.raises(ValueError, match="用户不存在或已停用"):
        commands(session_factory).dispatch(issue_id, inactive_id)
    with pytest.raises(ValueError, match="用户不存在或已停用"):
        commands(session_factory).dispatch(issue_id, 999)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_dispatch_rejects_unknown_issue(session_factory) -> None:
    with session_factory() as session:
        admin_id = seed_user(session).id

    with pytest.raises(ValueError, match="Issue 不存在"):
        commands(session_factory).dispatch(999, admin_id)


def test_dispatch_failure_rolls_back(session_factory) -> None:
    with session_factory() as session:
        task = seed_task(session)
        issue_id, admin_id = task.issue_id, task.created_by

    with pytest.raises(ValueError):
        commands(session_factory).dispatch(issue_id, admin_id)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_add_and_dispatch_creates_issue_and_task(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        admin_id = issue.repository.created_by

    task_id = commands(session_factory).add_and_dispatch(
        "https://github.com/octo/demo/issues/7", admin_id
    )

    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.issue.number == 7
        assert task.issue.triage_state == "dispatched"


def test_dispatch_in_session_leaves_commit_to_caller(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        issue_id, admin_id = issue.id, issue.repository.created_by

    with session_factory() as session:
        commands(session_factory).dispatch_in_session(session, issue_id, admin_id)
        session.rollback()

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_ignore_and_restore_round_trip(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        issue_id, admin_id = issue.id, issue.repository.created_by
        repository_id = issue.repository_id

    ref = commands(session_factory).ignore(issue_id, admin_id, "  重复问题  ")
    assert (ref.number, ref.repository_id) == (1, repository_id)
    with session_factory() as session:
        stored = session.get(Issue, issue_id)
        assert stored.triage_state == "ignored"
        assert stored.ignored_by == admin_id
        assert stored.ignored_reason == "重复问题"
        assert stored.ignored_at is not None

    commands(session_factory).restore(issue_id, admin_id)
    with session_factory() as session:
        stored = session.get(Issue, issue_id)
        assert stored.triage_state == "discovered"
        assert stored.ignored_by is None
        assert stored.ignored_reason is None
        assert stored.ignored_at is None


def test_ignore_requires_discovered_open_issue(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        issue.triage_state = "dispatched"
        session.commit()
        issue_id, admin_id = issue.id, issue.repository.created_by

    with pytest.raises(Conflict, match="只有待处理 Issue 可以忽略"):
        commands(session_factory).ignore(issue_id, admin_id)


def test_restore_requires_ignored_issue(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        issue_id, admin_id = issue.id, issue.repository.created_by

    with pytest.raises(Conflict, match="只有已忽略 Issue 可以恢复"):
        commands(session_factory).restore(issue_id, admin_id)


def test_ignore_rejects_non_admin_and_unknown_issue(session_factory) -> None:
    with session_factory() as session:
        issue = seed_issue(session)
        member = seed_user(session, username="member", role="user")
        issue_id, member_id, admin_id = (
            issue.id,
            member.id,
            issue.repository.created_by,
        )

    with pytest.raises(Forbidden):
        commands(session_factory).ignore(issue_id, member_id)
    with pytest.raises(NotFound):
        commands(session_factory).ignore(999, admin_id)
