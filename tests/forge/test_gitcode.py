from pathlib import Path
from types import SimpleNamespace

import pytest

from coderus.db import create_session_factory
from coderus.forge import GitCodeForge, PublishRequest
from coderus.models import Repository, User


class FakePublisher:
    calls: list[dict[str, object]] = []

    def __init__(self, token, account_name, *, registered_forks):
        self.account_name = account_name
        self.registered_forks = registered_forks

    def ensure_fork(self, owner, name):
        return SimpleNamespace(
            url=f"https://gitcode.com/{self.account_name}/{name}.git",
            owner=self.account_name,
        )

    def publish(self, **kwargs):
        assert self.registered_forks == {
            ("open", "widgets"): "https://gitcode.com/coderus-bot/widgets.git"
        }
        return SimpleNamespace(
            url="https://gitcode.com/open/widgets/pulls/1", number=1, state="open"
        )

    def get_pull_request(self, owner, name, number):
        self.calls.append(
            {
                "method": "get_pull_request",
                "registered_forks": self.registered_forks,
                "owner": owner,
                "name": name,
                "number": number,
            }
        )
        return SimpleNamespace(number=number, base_sha="a" * 40)

    def publish_pr_comment(self, owner, name, number, body, marker):
        self.calls.append(
            {
                "method": "publish_pr_comment",
                "registered_forks": self.registered_forks,
                "owner": owner,
                "name": name,
                "number": number,
                "body": body,
                "marker": marker,
            }
        )
        return SimpleNamespace(
            url=f"https://gitcode.com/{owner}/{name}/pulls/{number}#note_1",
            created=True,
        )


def add_repository(
    sessions,
    *,
    provider: str = "gitcode",
    fork_owner: str | None = None,
    fork_url: str | None = None,
    is_enabled: bool = True,
) -> None:
    with sessions() as session:
        user = User(username="admin", password_hash="hash", role="admin")
        session.add(
            Repository(
                provider=provider,
                owner="open",
                name="widgets",
                canonical_url=(
                    f"https://{'gitcode.com' if provider == 'gitcode' else 'github.com'}"
                    "/open/widgets"
                ),
                default_branch="main",
                fork_owner=fork_owner,
                fork_url=fork_url,
                is_enabled=is_enabled,
                created_by_user=user,
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_forge_loads_only_gitcode_registered_fork(engine, tmp_path: Path) -> None:
    sessions = create_session_factory(engine)
    add_repository(
        sessions,
        fork_owner="coderus-bot",
        fork_url="https://gitcode.com/coderus-bot/widgets.git",
    )
    forge = GitCodeForge(
        "token",
        "coderus-bot",
        session_factory=sessions,
        publisher_factory=FakePublisher,
    )

    result = await forge.publish(
        PublishRequest(
            workspace=tmp_path,
            upstream_owner="open",
            repository_name="widgets",
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
    add_repository(sessions)
    forge = GitCodeForge(
        "token",
        "coderus-bot",
        session_factory=sessions,
        publisher_factory=FakePublisher,
    )

    await forge.publish(
        PublishRequest(
            workspace=tmp_path,
            upstream_owner="open",
            repository_name="widgets",
            default_branch="main",
            branch="coderus/issue-1-1",
            title="Issue",
            body="Body",
        )
    )

    with sessions() as session:
        repository = session.query(Repository).one()
        assert repository.fork_owner == "coderus-bot"
        assert repository.fork_url == "https://gitcode.com/coderus-bot/widgets.git"


@pytest.mark.asyncio
async def test_forge_allows_pr_metadata_without_registered_fork(engine) -> None:
    sessions = create_session_factory(engine)
    add_repository(sessions)
    FakePublisher.calls = []
    forge = GitCodeForge(
        "token",
        "coderus-bot",
        session_factory=sessions,
        publisher_factory=FakePublisher,
    )

    details = await forge.get_pull_request("open", "widgets", 7)
    comment = await forge.publish_pr_comment(
        "open",
        "widgets",
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
async def test_forge_rejects_wrong_provider_or_disabled_repository(engine) -> None:
    sessions = create_session_factory(engine)
    add_repository(sessions, provider="github")
    forge = GitCodeForge(
        "token",
        "coderus-bot",
        session_factory=sessions,
        publisher_factory=FakePublisher,
    )

    with pytest.raises(ValueError, match="GitCode"):
        await forge.get_pull_request("open", "widgets", 7)
