import asyncio

import pytest

from coderus.db import create_session_factory
from coderus.models import Issue, Repository, Task, User
from coderus.workflow.scheduler import TaskScheduler


class BlockingOrchestrator:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.release = asyncio.Event()

    async def run(self, task_id: int) -> None:
        self.started.append(task_id)
        await self.release.wait()


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


def test_scheduler_recovers_interrupted_task_to_manual_intervention(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        add_queued_tasks(session, 1)
        task = session.get(Task, 1)
        task.status = "preparing"
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
