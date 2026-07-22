import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from coderus.db import create_session_factory
from coderus.models import AgentRun, Issue, Repository, Task, User
from coderus.workflow.scheduler import TaskScheduler


class BlockingOrchestrator:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.release = asyncio.Event()
        self.cancelled = False

    async def run(self, task_id: int, *, claim_token: str | None = None) -> None:
        assert claim_token
        self.started.append(task_id)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def add_queued_tasks(session, count: int) -> None:
    user = User(username="user", password_hash="hash")
    repository = Repository(
        provider="github",
        owner="octo",
        name="demo",
        canonical_url="https://github.com/octo/demo",
        default_branch="main",
        created_by_user=user,
    )
    for number in range(1, count + 1):
        issue = Issue(
            repository=repository,
            external_id=str(number),
            number=number,
            title=f"Issue {number}",
            state="open",
        )
        session.add(Task(issue=issue, creator=user, status="queued"))
    session.commit()


@pytest.mark.asyncio
async def test_scheduler_enforces_per_user_limit(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 3)
    orchestrator = BlockingOrchestrator()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=orchestrator,
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )

    await scheduler.tick()
    await asyncio.sleep(0)

    assert len(orchestrator.started) == 2
    orchestrator.release.set()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_does_not_claim_tasks_while_release_is_draining(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
    orchestrator = BlockingOrchestrator()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=orchestrator,
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
        can_claim=lambda: False,
    )

    await scheduler.tick()
    await asyncio.sleep(0)

    assert orchestrator.started == []


@pytest.mark.asyncio
async def test_scheduler_recovers_expired_tasks_when_new_claims_are_disabled(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        task = session.get(Task, 1)
        task.status = "preparing"
        task.claim_token = "expired"
        task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(AgentRun(task_id=task.id, role="developer", status="running"))
        session.commit()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=BlockingOrchestrator(),
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
        can_claim=lambda: False,
    )

    await scheduler.tick()

    with sessions() as session:
        assert session.get(Task, 1).status == "manual_intervention"


@pytest.mark.asyncio
async def test_scheduler_stop_cancels_and_drains_workers(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
    orchestrator = BlockingOrchestrator()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=orchestrator,
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )
    await scheduler.tick()
    await asyncio.sleep(0)

    await asyncio.wait_for(scheduler.stop(), timeout=0.5)

    assert orchestrator.cancelled is True


def test_scheduler_recovers_interrupted_task_to_manual_intervention(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        task = session.get(Task, 1)
        task.status = "preparing"
        task.claim_token = "expired"
        task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(
            AgentRun(task_id=task.id, role="developer", status="running")
        )
        session.commit()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=BlockingOrchestrator(),
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )

    assert scheduler.recover_interrupted() == 1

    with sessions() as session:
        task = session.get(Task, 1)
        assert task.status == "manual_intervention"
        assert task.failure_code == "manager_restarted"
        assert task.claim_token is None
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status == "interrupted"
        assert run.finished_at is not None


def test_scheduler_requeues_expired_publication_for_idempotent_reconciliation(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        task = session.get(Task, 1)
        task.status = "publishing"
        task.claim_token = "expired"
        task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        task.workspace_path = "workspaces/task-1"
        task.branch_name = "coderus/issue-1-1"
        task.commit_sha = "a" * 40
        session.add(AgentRun(task_id=task.id, role="developer", status="running"))
        session.commit()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=BlockingOrchestrator(),
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )

    assert scheduler.recover_interrupted() == 1

    with sessions() as session:
        task = session.get(Task, 1)
        assert task.status == "queued"
        assert task.failure_code == "publish_existing"
        assert task.claim_token is None
        assert task.finished_at is None


def test_scheduler_recovers_expired_cancelling_task_to_cancelled(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        task = session.get(Task, 1)
        task.status = "cancelling"
        task.claim_token = "expired"
        task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.add(AgentRun(task_id=task.id, role="developer", status="running"))
        session.commit()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=BlockingOrchestrator(),
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )

    assert scheduler.recover_interrupted() == 1

    with sessions() as session:
        task = session.get(Task, 1)
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert task.status == "cancelled"
        assert task.claim_token is None
        assert run.status == "interrupted"


def test_scheduler_recovers_cancelling_even_with_a_live_lease(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        task = session.get(Task, 1)
        task.status = "cancelling"
        task.claim_token = "current-owner"
        task.claim_expires_at = datetime.now(UTC) + timedelta(minutes=2)
        session.add(AgentRun(task_id=task.id, role="developer", status="running"))
        session.commit()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=BlockingOrchestrator(),
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )

    assert scheduler.recover_interrupted() == 1

    with sessions() as session:
        task = session.get(Task, 1)
        assert task.status == "cancelled"
        assert task.claim_token is None


def test_scheduler_preserves_running_task_with_a_live_lease(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        task = session.get(Task, 1)
        task.status = "preparing"
        task.claim_token = "current-owner"
        task.claim_expires_at = datetime.now(UTC) + timedelta(minutes=2)
        session.add(AgentRun(task_id=task.id, role="developer", status="running"))
        session.commit()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=BlockingOrchestrator(),
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )

    assert scheduler.recover_interrupted() == 0

    with sessions() as session:
        task = session.get(Task, 1)
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert task.status == "preparing"
        assert run.status == "running"


@pytest.mark.asyncio
async def test_scheduler_does_not_start_disabled_users_tasks(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        user = session.get(User, 1)
        user.is_active = False
        session.commit()
    orchestrator = BlockingOrchestrator()
    scheduler = TaskScheduler(
        session_factory=sessions,
        orchestrator=orchestrator,
        global_limit=8,
        per_user_limit=2,
        poll_seconds=30,
    )

    await scheduler.tick()
    await asyncio.sleep(0)

    assert orchestrator.started == []
