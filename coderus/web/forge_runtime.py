from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.forge import (
    ForgeRegistration,
    ForkResult,
    GitCodeForge,
    GitCodeProvider,
    GitHubForge,
    GitHubProvider,
    ProviderName,
)
from coderus.models import Repository

_PROVIDER_LABELS: dict[str, str] = {"github": "GitHub", "gitcode": "GitCode"}


class DatabaseForkRegistry:
    """基于仓库登记表的 Fork 查询与登记，实现 forge 的 ForkRegistry 协议。"""

    def __init__(
        self, session_factory: Callable[[], Session], provider: ProviderName
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._label = _PROVIDER_LABELS.get(provider, provider)

    def fork_url(self, owner: str, name: str) -> str | None:
        with self._sessions() as session:
            return self._repository(session, owner, name).fork_url

    def record_fork(self, owner: str, name: str, fork: ForkResult) -> None:
        with self._sessions() as session:
            repository = self._repository(session, owner, name)
            repository.fork_owner = fork.owner
            repository.fork_url = fork.url
            session.commit()

    def _repository(self, session: Session, owner: str, name: str) -> Repository:
        repository = session.scalar(
            select(Repository).where(
                Repository.provider == self._provider,
                Repository.owner == owner,
                Repository.name == name,
                Repository.is_enabled.is_(True),
            )
        )
        if repository is None:
            raise ValueError(f"{self._label} 仓库尚未登记")
        return repository


@dataclass(frozen=True)
class ForgeRuntime:
    provider_client: object
    registration: ForgeRegistration


def build_github_runtime(
    token: str,
    *,
    client: object,
    session_factory,
) -> ForgeRuntime:
    return ForgeRuntime(
        provider_client=GitHubProvider(client=client, token=token),
        registration=ForgeRegistration.full(
            GitHubForge(
                token, forks=DatabaseForkRegistry(session_factory, "github")
            )
        ),
    )


def build_gitcode_runtime(
    token: str,
    *,
    account_name: str | None,
    client: object,
    session_factory,
) -> ForgeRuntime:
    return ForgeRuntime(
        provider_client=GitCodeProvider(client=client, token=token),
        registration=(
            ForgeRegistration.full(
                GitCodeForge(
                    token,
                    account_name,
                    forks=DatabaseForkRegistry(session_factory, "gitcode"),
                    http_client=client,
                )
            )
            if account_name is not None
            else ForgeRegistration.unavailable()
        ),
    )


def install_forge_runtime(app, provider: ProviderName, runtime: ForgeRuntime) -> None:
    app.state.providers[provider] = runtime.provider_client
    if app.state.issue_poller.providers is not app.state.providers:
        app.state.issue_poller.providers[provider] = runtime.provider_client
    app.state.forges.install_registration(provider, runtime.registration)
