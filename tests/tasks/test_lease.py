from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from coderus.models import Issue, Repository, Task, User
from coderus.tasks.lease import TaskLease, heartbeat_loop


def seed_task(session: Session, *, claim_token: str | None = "token") -> Task:
    user = User(username="admin", password_hash="hash", role="admin")
    repository = Repository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        created_by_user=user,
    )
    issue = Issue(
        repository=repository,
        external_id="1",
        number=1,
        title="crash",
        body="",
        state="open",
        source_url="https://github.com/octo/demo/issues/1",
        triage_state="dispatched",
    )
    task = Task(
        issue=issue,
        creator=user,
        status="preparing",
        claim_token=claim_token,
        claim_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    session.add(task)
    session.commit()
    return task


def lease(engine) -> TaskLease:
    return TaskLease(
        session_factory=lambda: Session(engine),
        model=Task,
        active_statuses=("preparing",),
    )


def test_renew_extends_lease_for_current_owner(engine) -> None:
    with Session(engine) as session:
        task_id = seed_task(session).id

    assert lease(engine).renew(task_id, "token") is True
    with Session(engine) as session:
        expires_at = session.get(Task, task_id).claim_expires_at.replace(tzinfo=UTC)
        assert expires_at > datetime.now(UTC) + timedelta(seconds=100)


def test_renew_rejects_wrong_owner_status_or_expiry(engine) -> None:
    with Session(engine) as session:
        task = seed_task(session)
        task_id = task.id

    assert lease(engine).renew(task_id, "other-token") is False

    with Session(engine) as session:
        session.get(Task, task_id).claim_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        session.commit()
    assert lease(engine).renew(task_id, "token") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["return_false", "raise"])
async def test_heartbeat_loop_reports_lost_lease(failure: str) -> None:
    lost = asyncio.Event()

    def renew() -> bool:
        if failure == "raise":
            raise RuntimeError("database unavailable")
        return False

    await asyncio.wait_for(
        heartbeat_loop(renew, asyncio.Event(), lost.set, interval=0.01),
        timeout=1,
    )
    assert lost.is_set()


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_cleanly_when_asked() -> None:
    stop = asyncio.Event()
    lost = asyncio.Event()
    renewals = 0

    def renew() -> bool:
        nonlocal renewals
        renewals += 1
        if renewals >= 2:
            stop.set()
        return True

    await asyncio.wait_for(
        heartbeat_loop(renew, stop, lost.set, interval=0.01), timeout=1
    )
    assert not lost.is_set()
    assert renewals >= 2
