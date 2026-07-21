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
