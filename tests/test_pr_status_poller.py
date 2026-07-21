import pytest

from coderus.db import create_session_factory
from coderus.forge import ForgeRegistry
from coderus.models import Issue, Repository, Task, User
from coderus.workflow.pr_status import PRStatusPoller


class FakePublisher:
    async def get_pr_status(self, owner: str, name: str, number: int) -> str:
        assert (owner, name) == ("octo", "demo")
        return {7: "merged", 8: "closed"}[number]


def add_task(session, user, repository, number: int) -> Task:
    issue = Issue(
        repository=repository,
        external_id=str(number),
        number=number,
        title=f"Issue {number}",
        state="open",
    )
    task = Task(
        issue=issue,
        creator=user,
        status="awaiting_human_review",
        pr_number=number,
        pr_state="open",
        pr_url=f"https://github.com/octo/demo/pull/{number}",
    )
    session.add(task)
    return task


class TrackingForge:
    def __init__(self, states: dict[int, str | Exception]) -> None:
        self.states = states
        self.calls: list[int] = []

    async def get_pr_status(self, owner: str, name: str, number: int) -> str:
        self.calls.append(number)
        result = self.states[number]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_pr_status_poller_marks_merged_and_closed_tasks(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            created_by_user=user,
        )
        merged = add_task(session, user, repository, 7)
        closed = add_task(session, user, repository, 8)
        session.commit()
        ids = (merged.id, closed.id)
    poller = PRStatusPoller(
        session_factory=sessions,
        forges=ForgeRegistry({"github": FakePublisher()}),
        poll_seconds=300,
    )

    await poller.tick()

    with sessions() as session:
        tasks = [session.get(Task, task_id) for task_id in ids]
        assert [(task.status, task.pr_state) for task in tasks] == [
            ("completed", "merged"),
            ("closed", "closed"),
        ]
        assert all(task.finished_at is not None for task in tasks)


@pytest.mark.asyncio
async def test_pr_status_poller_isolates_mixed_forge_failures_and_skips_dismissed(
    engine,
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash")
        github = Repository(
            provider="github",
            owner="octo",
            name="github-demo",
            canonical_url="https://github.com/octo/github-demo",
            default_branch="main",
            created_by_user=user,
        )
        gitcode = Repository(
            provider="gitcode",
            owner="open",
            name="gitcode-demo",
            canonical_url="https://gitcode.com/open/gitcode-demo",
            default_branch="main",
            created_by_user=user,
        )
        unavailable = Repository(
            provider="unconfigured",
            owner="missing",
            name="demo",
            canonical_url="https://example.com/missing/demo",
            default_branch="main",
            created_by_user=user,
        )
        merged = add_task(session, user, github, 7)
        closed = add_task(session, user, gitcode, 8)
        waiting_unconfigured = add_task(session, user, unavailable, 9)
        waiting_transient_failure = add_task(session, user, github, 10)
        dismissed = add_task(session, user, gitcode, 11)
        dismissed.status = "dismissed"
        session.commit()
        ids = (
            merged.id,
            closed.id,
            waiting_unconfigured.id,
            waiting_transient_failure.id,
            dismissed.id,
        )
    github_forge = TrackingForge({7: "merged", 10: RuntimeError("temporary")})
    gitcode_forge = TrackingForge({8: "closed", 11: "merged"})
    poller = PRStatusPoller(
        session_factory=sessions,
        forges=ForgeRegistry({"github": github_forge, "gitcode": gitcode_forge}),
        poll_seconds=300,
    )

    await poller.tick()

    with sessions() as session:
        tasks = [session.get(Task, task_id) for task_id in ids]
        assert [(task.status, task.pr_state) for task in tasks] == [
            ("completed", "merged"),
            ("closed", "closed"),
            ("awaiting_human_review", "open"),
            ("awaiting_human_review", "open"),
            ("dismissed", "open"),
        ]
    assert github_forge.calls == [7, 10]
    assert gitcode_forge.calls == [8]


@pytest.mark.asyncio
async def test_pr_status_poller_can_enable_forge_after_start(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            created_by_user=user,
        )
        task = add_task(session, user, repository, 7)
        session.commit()
        task_id = task.id
    forges = ForgeRegistry()
    poller = PRStatusPoller(session_factory=sessions, forges=forges, poll_seconds=300)

    poller.start()
    assert poller._loop_task is not None
    forges.install("github", FakePublisher())
    await poller.tick()
    await poller.stop()

    with sessions() as session:
        assert session.get(Task, task_id).status == "completed"


@pytest.mark.asyncio
async def test_pr_status_poller_skips_github_tasks_when_forge_is_removed(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            created_by_user=user,
        )
        task = add_task(session, user, repository, 7)
        session.commit()
        task_id = task.id
    forges = ForgeRegistry({"github": FakePublisher()})
    poller = PRStatusPoller(session_factory=sessions, forges=forges, poll_seconds=300)

    forges.remove("github")
    await poller.tick()

    with sessions() as session:
        assert session.get(Task, task_id).status == "awaiting_human_review"
