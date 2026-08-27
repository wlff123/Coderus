from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.forge.protocols import PublishRequest
from coderus.models import Repository
from coderus.publisher import GitHubPublisher


class GitHubForge:
    def __init__(
        self,
        token: str,
        *,
        session_factory: Callable[[], Session],
        publisher_factory: type = GitHubPublisher,
    ) -> None:
        self._token = token
        self._sessions = session_factory
        self._publisher_factory = publisher_factory

    async def ensure_fork(self, owner: str, name: str):
        publisher = self._publisher_factory(self._token, registered_forks={})
        return await asyncio.to_thread(publisher.ensure_fork, owner, name)

    async def publish(self, request: PublishRequest):
        return await asyncio.to_thread(self._publish, request)

    async def list_pr_feedback(self, owner: str, name: str, pr_number: int):
        publisher = self._registered_publisher(owner, name)
        return await asyncio.to_thread(
            publisher.list_pr_feedback, owner, name, pr_number
        )

    async def get_pr_status(self, owner: str, name: str, pr_number: int):
        publisher = self._registered_publisher(owner, name)
        return await asyncio.to_thread(publisher.get_pr_status, owner, name, pr_number)

    async def get_pull_request(self, owner: str, name: str, pr_number: int):
        publisher = self._registered_publisher(owner, name)
        return await asyncio.to_thread(publisher.get_pull_request, owner, name, pr_number)

    async def publish_pr_comment(
        self, owner: str, name: str, pr_number: int, body: str, marker: str
    ):
        publisher = self._registered_publisher(owner, name)
        return await asyncio.to_thread(
            publisher.publish_pr_comment, owner, name, pr_number, body, marker
        )

    def _publish(self, request: PublishRequest):
        publisher = self._registered_publisher(
            request.upstream_owner, request.repository_name, register_fork=True
        )
        return publisher.publish(
            workspace=request.workspace,
            upstream_owner=request.upstream_owner,
            repository_name=request.repository_name,
            default_branch=request.default_branch,
            branch=request.branch,
            title=request.title,
            body=request.body,
        )

    def _registered_publisher(
        self, owner: str, name: str, *, register_fork: bool = False
    ):
        with self._sessions() as session:
            repository = session.scalar(
                select(Repository).where(
                    Repository.provider == "github",
                    Repository.owner == owner,
                    Repository.name == name,
                    Repository.is_enabled.is_(True),
                )
            )
            if repository is None:
                raise ValueError("GitHub 仓库尚未登记")
            if not repository.fork_url and register_fork:
                bootstrap = self._publisher_factory(self._token, registered_forks={})
                fork = bootstrap.ensure_fork(owner, name)
                repository.fork_owner = fork.owner
                repository.fork_url = fork.url
                session.commit()
            registered = (
                {(owner, name): repository.fork_url}
                if repository.fork_url
                else {}
            )
        return self._publisher_factory(self._token, registered_forks=registered)
