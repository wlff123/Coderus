from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from coderus.models import Issue, Repository, Task, User


@pytest.fixture
def session_factory(engine):
    return lambda: Session(engine)


def seed_user(
    session: Session,
    *,
    username: str = "admin",
    role: str = "admin",
    is_active: bool = True,
) -> User:
    user = User(
        username=username,
        password_hash="hash",
        role=role,
        is_active=is_active,
    )
    session.add(user)
    session.commit()
    return user


def seed_issue(session: Session, *, creator: User | None = None) -> Issue:
    creator = creator or seed_user(session)
    repository = Repository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        created_by_user=creator,
    )
    issue = Issue(
        repository=repository,
        external_id="1",
        number=1,
        title="crash on start",
        body="details",
        state="open",
        source_url="https://github.com/octo/demo/issues/1",
    )
    session.add(issue)
    session.commit()
    return issue


def seed_task(session: Session, *, status: str = "queued") -> Task:
    issue = seed_issue(session)
    issue.triage_state = "dispatched"
    task = Task(issue=issue, creator=issue.repository.created_by_user, status=status)
    session.add(task)
    session.commit()
    return task
