from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from coderus.application.errors import Conflict, Forbidden, NotFound
from coderus.application.repositories import RepositoryCommands, SyncFailed
from coderus.forge import ForgeCapability, ForgeRegistration, ForgeRegistry
from coderus.models import Repository
from coderus.providers.models import Repository as ProviderRepository

from .conftest import seed_issue, seed_user


class FakeProvider:
    name = "github"

    def __init__(
        self,
        *,
        is_private: bool = False,
        issues_enabled: bool = True,
        list_error: Exception | None = None,
    ) -> None:
        self._is_private = is_private
        self._issues_enabled = issues_enabled
        self._list_error = list_error

    def get_repository(self, url: str) -> ProviderRepository:
        owner, name = url.rstrip("/").split("/")[-2:]
        return ProviderRepository(
            provider="github",
            owner=owner,
            name=name,
            canonical_url=url,
            default_branch="main",
            is_private=self._is_private,
            issues_enabled=self._issues_enabled,
        )

    def list_open_issues(self, repository) -> list:
        if self._list_error is not None:
            raise self._list_error
        return []


class FakeFork:
    owner = "bot"
    url = "https://github.com/bot/demo"


class FakeForge:
    def __init__(self) -> None:
        self.fork_calls: list[tuple[str, str]] = []

    async def ensure_fork(self, owner: str, name: str) -> FakeFork:
        self.fork_calls.append((owner, name))
        return FakeFork()


def commands(
    session_factory,
    *,
    provider: FakeProvider | None = None,
    forge: FakeForge | None = None,
) -> RepositoryCommands:
    forges = ForgeRegistry(
        {
            "github": ForgeRegistration(
                forge=forge, capabilities=frozenset({ForgeCapability.ENSURE_FORK})
            )
        }
        if forge is not None
        else {}
    )
    return RepositoryCommands(
        session_factory=session_factory,
        providers={"github": provider or FakeProvider()},
        forges=forges,
        error_formatter=lambda exc: f"formatted: {exc}",
    )


def test_add_registers_repository_with_fork(session_factory) -> None:
    with session_factory() as session:
        admin_id = seed_user(session).id
    forge = FakeForge()

    ref = asyncio.run(
        commands(session_factory, forge=forge).add(
            "https://github.com/octo/widgets", admin_id
        )
    )

    assert (ref.owner, ref.name, ref.is_enabled) == ("octo", "widgets", True)
    assert forge.fork_calls == [("octo", "widgets")]
    with session_factory() as session:
        stored = session.scalar(
            select(Repository).where(Repository.name == "widgets")
        )
        assert stored.fork_owner == "bot"
        assert stored.created_by == admin_id


def test_add_rejects_private_or_issueless_repository(session_factory) -> None:
    with session_factory() as session:
        admin_id = seed_user(session).id

    with pytest.raises(ValueError, match="仓库必须公开且启用 Issue"):
        asyncio.run(
            commands(session_factory, provider=FakeProvider(is_private=True)).add(
                "https://github.com/octo/private", admin_id
            )
        )


def test_add_requires_admin(session_factory) -> None:
    with session_factory() as session:
        member_id = seed_user(session, username="member", role="user").id

    with pytest.raises(Forbidden):
        asyncio.run(
            commands(session_factory).add(
                "https://github.com/octo/widgets", member_id
            )
        )


def test_sync_records_formatted_failure(session_factory) -> None:
    with session_factory() as session:
        repository_id = seed_issue(session).repository_id

    failing = FakeProvider(list_error=RuntimeError("status 403"))
    with pytest.raises(SyncFailed, match="formatted: status 403"):
        commands(session_factory, provider=failing).sync(repository_id)

    with session_factory() as session:
        stored = session.get(Repository, repository_id)
        assert stored.sync_status == "failed"
        assert stored.last_sync_error == "formatted: status 403"


def test_sync_rejects_running_or_unknown_repository(session_factory) -> None:
    with session_factory() as session:
        repository_id = seed_issue(session).repository_id
        session.get(Repository, repository_id).sync_status = "running"
        session.commit()

    with pytest.raises(Conflict, match="仓库正在同步"):
        commands(session_factory).sync(repository_id)
    with pytest.raises(NotFound):
        commands(session_factory).sync(999)


def test_toggle_flips_enabled_state(session_factory) -> None:
    with session_factory() as session:
        repository_id = seed_issue(session).repository_id

    ref = commands(session_factory).toggle(repository_id)
    assert ref.is_enabled is False
    ref = commands(session_factory).toggle(repository_id)
    assert ref.is_enabled is True
