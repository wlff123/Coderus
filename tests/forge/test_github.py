from pathlib import Path
from types import SimpleNamespace

import pytest

from coderus.db import create_session_factory
from coderus.forge import GitHubForge, PublishRequest
from coderus.models import Repository, User


class FakePublisher:
    calls: list[dict[str, object]] = []

    def __init__(self, token, *, registered_forks):
        self.registered_forks = registered_forks

    def ensure_fork(self, owner, name):
        return SimpleNamespace(url=f"https://github.com/bot/{name}.git", owner="bot")

    def publish(self, **kwargs):
        assert self.registered_forks == {("octo", "demo"): "https://github.com/bot/demo.git"}
        return SimpleNamespace(url="https://github.com/octo/demo/pull/1", number=1, state="open")

    def get_pull_request(self, owner, name, number):
        self.calls.append({
            "method": "get_pull_request",
            "registered_forks": self.registered_forks,
            "owner": owner,
            "name": name,
            "number": number,
        })
        return SimpleNamespace(number=number, base_sha="a" * 40)

    def publish_pr_comment(self, owner, name, number, body, marker):
        self.calls.append({
            "method": "publish_pr_comment",
            "registered_forks": self.registered_forks,
            "owner": owner,
            "name": name,
            "number": number,
            "body": body,
            "marker": marker,
        })
        return SimpleNamespace(
            url=f"https://github.com/{owner}/{name}/pull/{number}#issuecomment-1", created=True
        )


@pytest.mark.asyncio
async def test_forge_loads_registered_fork_from_database(engine, tmp_path: Path) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider="github",
                owner="octo",
                name="demo",
                canonical_url="https://github.com/octo/demo",
                default_branch="main",
                fork_owner="bot",
                fork_url="https://github.com/bot/demo.git",
                created_by_user=user,
            )
        )
        session.commit()
    forge = GitHubForge("token", session_factory=sessions, publisher_factory=FakePublisher)

    result = await forge.publish(
        PublishRequest(
            workspace=tmp_path,
            upstream_owner="octo",
            repository_name="demo",
            default_branch="main",
            branch="coderus/issue-1-1",
            title="Issue",
            body="Body",
        )
    )

    assert result.number == 1


@pytest.mark.asyncio
async def test_forge_registers_fork_on_first_publish(engine, tmp_path: Path) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider="github", owner="octo", name="demo",
                canonical_url="https://github.com/octo/demo", default_branch="main",
                created_by_user=user,
            )
        )
        session.commit()
    forge = GitHubForge("token", session_factory=sessions, publisher_factory=FakePublisher)

    await forge.publish(
        PublishRequest(
            workspace=tmp_path, upstream_owner="octo", repository_name="demo",
            default_branch="main", branch="coderus/issue-1-1",
            title="Issue", body="Body",
        )
    )

    with sessions() as session:
        repository = session.query(Repository).one()
        assert repository.fork_owner == "bot"
        assert repository.fork_url == "https://github.com/bot/demo.git"


@pytest.mark.asyncio
async def test_forge_allows_pr_metadata_and_comment_without_registered_fork(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider="github",
                owner="octo",
                name="demo",
                canonical_url="https://github.com/octo/demo",
                default_branch="main",
                created_by_user=user,
            )
        )
        session.commit()
    FakePublisher.calls = []
    forge = GitHubForge("token", session_factory=sessions, publisher_factory=FakePublisher)

    details = await forge.get_pull_request("octo", "demo", 7)
    comment = await forge.publish_pr_comment(
        "octo",
        "demo",
        7,
        "review\n<!-- coderus-pr-review:RV-7:abc -->",
        "<!-- coderus-pr-review:RV-7:abc -->",
    )

    assert details.number == 7
    assert comment.created is True
    assert [call["method"] for call in FakePublisher.calls] == [
        "get_pull_request",
        "publish_pr_comment",
    ]
    assert all(call["registered_forks"] == {} for call in FakePublisher.calls)


@pytest.mark.asyncio
async def test_forge_rejects_disabled_repository_for_pr_metadata(engine) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider="github",
                owner="octo",
                name="demo",
                canonical_url="https://github.com/octo/demo",
                default_branch="main",
                fork_owner="bot",
                fork_url="https://github.com/bot/demo.git",
                is_enabled=False,
                created_by_user=user,
            )
        )
        session.commit()
    FakePublisher.calls = []
    forge = GitHubForge("token", session_factory=sessions, publisher_factory=FakePublisher)

    with pytest.raises(ValueError, match="GitHub"):
        await forge.get_pull_request("octo", "demo", 7)

    assert FakePublisher.calls == []
