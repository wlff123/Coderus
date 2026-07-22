import pytest

from coderus.db import create_session_factory
from coderus.issues.poller import IssuePoller
from coderus.models import Repository, User
from tests.test_issue_service import provider_issue


@pytest.mark.asyncio
async def test_poller_syncs_enabled_repository(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            created_by_user=user,
        )
        session.add(repository)
        session.commit()

    class Provider:
        def list_issues(self, repository, *, state):
            assert state == "all"
            return [provider_issue()]

    poller = IssuePoller(
        session_factory=sessions,
        providers={"github": Provider()},
        poll_seconds=300,
    )

    await poller.tick()

    with sessions() as session:
        repository = session.get(Repository, 1)
        assert repository.sync_status == "succeeded"
        assert len(repository.issues) == 1


@pytest.mark.asyncio
async def test_poller_does_not_sync_while_release_is_draining(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            created_by_user=user,
        )
        session.add(repository)
        session.commit()

    class Provider:
        def list_issues(self, repository, *, state):
            raise AssertionError("repository sync must not start")

    poller = IssuePoller(
        session_factory=sessions,
        providers={"github": Provider()},
        poll_seconds=300,
        can_run=lambda: False,
    )

    await poller.tick()


@pytest.mark.asyncio
async def test_poller_runs_periodic_full_sync_between_incremental_syncs(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            created_by_user=user,
        )
        session.add(repository)
        session.commit()

    cursors: list[object] = []

    class Provider:
        def list_issues(self, repository, *, state, updated_since=None):
            cursors.append(updated_since)
            return [provider_issue()]

    poller = IssuePoller(
        session_factory=sessions,
        providers={"github": Provider()},
        poll_seconds=300,
        full_sync_every=2,
    )

    await poller.tick()
    await poller.tick()
    await poller.tick()

    assert cursors[0] is None
    assert cursors[1] is not None
    assert cursors[2] is None


@pytest.mark.asyncio
async def test_poller_persists_and_logs_provider_failure(engine, caplog) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
            created_by_user=user,
        )
        session.add(repository)
        session.commit()

    class Provider:
        def list_issues(self, repository, *, state):
            raise RuntimeError("remote unavailable")

    poller = IssuePoller(
        session_factory=sessions,
        providers={"github": Provider()},
        poll_seconds=300,
    )

    with caplog.at_level("ERROR"):
        await poller.tick()

    with sessions() as session:
        repository = session.get(Repository, 1)
        assert repository.sync_status == "failed"
        assert repository.last_sync_error == "remote unavailable"
    record = next(record for record in caplog.records if record.msg == "issue_sync_failed")
    assert record.provider == "github"
    assert record.repository_id == 1
    assert record.error_type == "RuntimeError"
