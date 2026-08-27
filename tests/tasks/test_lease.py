from __future__ import annotations

import asyncio
import threading
import time
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
        heartbeat_loop(
            renew, threading.Event(), lost.set, interval=0.01, lease_seconds=0.05
        ),
        timeout=2,
    )
    await asyncio.wait_for(lost.wait(), timeout=1)


@pytest.mark.asyncio
async def test_heartbeat_loop_tolerates_transient_renew_errors() -> None:
    stop = threading.Event()
    lost = asyncio.Event()
    calls = 0

    def renew() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database busy")
        if calls >= 3:
            stop.set()
        return True

    await asyncio.wait_for(
        heartbeat_loop(renew, stop, lost.set, interval=0.01, lease_seconds=60),
        timeout=2,
    )
    assert not lost.is_set()
    assert calls >= 3


@pytest.mark.asyncio
async def test_heartbeat_loop_renews_while_event_loop_is_blocked() -> None:
    """事件循环被同步工作阻塞时，续约必须照常发生。"""
    stop = threading.Event()
    lost = asyncio.Event()
    renewals = 0

    def renew() -> bool:
        nonlocal renewals
        renewals += 1
        return True

    loop_task = asyncio.create_task(
        heartbeat_loop(renew, stop, lost.set, interval=0.01, lease_seconds=60)
    )
    await asyncio.sleep(0.05)  # 让心跳线程先启动
    time.sleep(0.3)  # 故意在事件循环线程上做同步阻塞
    blocked_renewals = renewals
    stop.set()
    await asyncio.wait_for(loop_task, timeout=2)
    assert blocked_renewals >= 5
    assert not lost.is_set()


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_cleanly_when_asked() -> None:
    stop = threading.Event()
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
