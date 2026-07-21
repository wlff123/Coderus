from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from coderus.models import PRReviewTask
from coderus.pr_review.scheduler import PRReviewScheduler
from tests.pr_review.test_orchestrator import (
    FakePublisher,
    add_review_task,
    build_orchestrator,
)


class BlockingOrchestrator:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[int] = []

    async def run(self, task_id: int) -> None:
        self.calls.append(task_id)
        self.started.set()
        await self.release.wait()


class ExplodingOrchestrator:
    def __init__(self, engine, *, secret: str) -> None:
        self.engine = engine
        self.secret = secret

    async def run(self, task_id: int) -> None:
        with Session(self.engine) as session:
            task = session.get(PRReviewTask, task_id)
            task.status = "reviewing"
            task.claim_token = "fresh-worker-token"
            task.claim_expires_at = datetime.now(UTC) + timedelta(seconds=30)
            session.commit()
        raise RuntimeError(self.secret)


@pytest.mark.asyncio
async def test_tick_starts_only_one_queued_review_at_a_time(engine, session: Session) -> None:
    first = add_review_task(session, suffix="1")
    second = add_review_task(session, suffix="2")
    orchestrator = BlockingOrchestrator()
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=orchestrator,
        poll_seconds=60,
    )

    await scheduler.tick()
    await orchestrator.started.wait()
    await scheduler.tick()

    assert orchestrator.calls == [first.id]
    assert second.id not in scheduler._running
    orchestrator.release.set()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_tick_does_not_claim_review_while_release_is_draining(
    engine, session: Session
) -> None:
    add_review_task(session)
    orchestrator = BlockingOrchestrator()
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=orchestrator,
        poll_seconds=60,
        can_claim=lambda: False,
    )

    await scheduler.tick()

    assert orchestrator.calls == []


def test_recover_expired_requeues_only_running_states_without_leases(
    engine, session: Session
) -> None:
    statuses = ["preparing", "reviewing", "commenting", "completed", "failed"]
    tasks = [add_review_task(session, suffix=str(index)) for index in range(1, 6)]
    now = datetime.now(UTC)
    for task, status in zip(tasks, statuses, strict=True):
        task.status = status
        task.started_at = now
        task.finished_at = now
        task.failure_code = "old-code"
        task.failure_summary = "old summary"
    session.commit()
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=BlockingOrchestrator(),
        poll_seconds=60,
    )

    assert scheduler.recover_expired() == 3

    session.expire_all()
    recovered = [session.get(PRReviewTask, task.id) for task in tasks]
    for task in recovered[:3]:
        assert task.status == "queued"
        assert task.started_at is None
        assert task.finished_at is None
        assert task.failure_code is None
        assert task.failure_summary is None
    assert [task.status for task in recovered[3:]] == ["completed", "failed"]
    assert all(task.failure_code == "old-code" for task in recovered[3:])


def test_recover_expired_preserves_fresh_lease(engine, session: Session) -> None:
    task = add_review_task(session)
    task.status = "reviewing"
    task.claim_token = "fresh-token"
    task.claim_expires_at = datetime.now(UTC) + timedelta(seconds=30)
    session.commit()
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=BlockingOrchestrator(),
        poll_seconds=60,
    )

    assert scheduler.recover_expired() == 0

    session.expire_all()
    persisted = session.get(PRReviewTask, task.id)
    assert persisted.status == "reviewing"
    assert persisted.claim_token == "fresh-token"


def test_recover_expired_requeues_expired_and_legacy_leases(
    engine, session: Session
) -> None:
    expired = add_review_task(session, suffix="expired")
    legacy = add_review_task(session, suffix="legacy")
    expired.status = "preparing"
    expired.claim_token = "expired-token"
    expired.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    legacy.status = "commenting"
    legacy.claim_token = None
    legacy.claim_expires_at = None
    session.commit()
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=BlockingOrchestrator(),
        poll_seconds=60,
    )

    assert scheduler.recover_expired() == 2

    session.expire_all()
    for task in (expired, legacy):
        persisted = session.get(PRReviewTask, task.id)
        assert persisted.status == "queued"
        assert persisted.claim_token is None
        assert persisted.claim_expires_at is None


@pytest.mark.asyncio
async def test_start_recovers_interrupted_tasks_before_polling(engine, session: Session) -> None:
    task = add_review_task(session)
    task.status = "reviewing"
    task.started_at = datetime.now(UTC)
    session.commit()
    orchestrator = BlockingOrchestrator()
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=orchestrator,
        poll_seconds=60,
    )

    scheduler.start()
    await orchestrator.started.wait()

    session.expire_all()
    assert session.get(PRReviewTask, task.id).status == "queued"
    assert orchestrator.calls == [task.id]
    orchestrator.release.set()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_stop_cancels_started_review(
    engine, session: Session, caplog
) -> None:
    task = add_review_task(session)
    orchestrator = BlockingOrchestrator()
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=orchestrator,
        poll_seconds=60,
    )
    await scheduler.tick()
    await orchestrator.started.wait()

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(scheduler.stop(), timeout=1)

    assert orchestrator.calls == [task.id]
    assert scheduler._running == {}
    assert "failed" not in caplog.text


@pytest.mark.asyncio
async def test_tick_logs_escaped_worker_exception_and_marks_active_task_failed(
    engine, session: Session, caplog
) -> None:
    task = add_review_task(session)
    secret = "worker-sensitive-error"
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=ExplodingOrchestrator(engine, secret=secret),
        poll_seconds=60,
    )

    with caplog.at_level(logging.ERROR):
        await scheduler.tick()
        await asyncio.sleep(0)
        await scheduler.tick()

    session.expire_all()
    persisted = session.get(PRReviewTask, task.id)
    assert persisted.status == "reviewing"
    assert persisted.failure_code is None
    assert persisted.claim_token == "fresh-worker-token"
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_stop_observes_worker_exception_in_gather_results(
    engine, session: Session, caplog
) -> None:
    task = add_review_task(session)
    secret = "shutdown-sensitive-error"
    scheduler = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=ExplodingOrchestrator(engine, secret=secret),
        poll_seconds=60,
    )
    await scheduler.tick()
    await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR):
        await scheduler.stop()

    session.expire_all()
    persisted = session.get(PRReviewTask, task.id)
    assert persisted.status == "reviewing"
    assert persisted.failure_code is None
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_schedulers_do_not_restart_fresh_lease_or_duplicate_codex(
    engine, session: Session, tmp_path
) -> None:
    task = add_review_task(session)

    class BlockingPublisher(FakePublisher):
        def __init__(self) -> None:
            super().__init__(engine)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def get_pull_request(self, owner: str, name: str, pr_number: int):
            if self.get_calls == 0:
                self.get_calls += 1
                self.started.set()
                await self.release.wait()
                return self.details
            return await super().get_pull_request(owner, name, pr_number)

    publisher = BlockingPublisher()
    orchestrator, _, _, runner, _, _ = build_orchestrator(
        engine, tmp_path, publisher=publisher
    )
    first = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=orchestrator,
        poll_seconds=60,
    )
    second = PRReviewScheduler(
        session_factory=lambda: Session(engine),
        orchestrator=orchestrator,
        poll_seconds=60,
    )

    await first.tick()
    await second.tick()
    await publisher.started.wait()
    with Session(engine) as other:
        persisted = other.get(PRReviewTask, task.id)
        assert persisted.status == "preparing"
        assert persisted.claim_token is not None
        expires_at = persisted.claim_expires_at
        assert expires_at is not None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        assert expires_at > datetime.now(UTC)
    publisher.release.set()
    await next(iter(first._running.values()))
    await first.stop()
    await second.stop()

    session.expire_all()
    assert session.get(PRReviewTask, task.id).status == "completed"
    assert len(runner.specs) == 1
