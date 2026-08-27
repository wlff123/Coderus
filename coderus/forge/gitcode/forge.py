from __future__ import annotations

import asyncio

from coderus.forge.gitcode.pulls import GitCodePublisher
from coderus.forge.protocols import ForkRegistry, PublishRequest


class GitCodeForge:
    def __init__(
        self,
        token: str,
        account_name: str,
        *,
        forks: ForkRegistry,
        publisher_factory: type = GitCodePublisher,
        http_client: object | None = None,
    ) -> None:
        self._token = token
        self._account_name = account_name
        self._forks = forks
        self._publisher_factory = publisher_factory
        self._http_client = http_client

    async def ensure_fork(self, owner: str, name: str):
        publisher = self._new_publisher(registered_forks={})
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
        fork_url = self._forks.fork_url(owner, name)
        if not fork_url and register_fork:
            bootstrap = self._new_publisher(registered_forks={})
            fork = bootstrap.ensure_fork(owner, name)
            self._forks.record_fork(owner, name, fork)
            fork_url = fork.url
        registered = {(owner, name): fork_url} if fork_url else {}
        return self._new_publisher(registered_forks=registered)

    def _new_publisher(self, *, registered_forks: dict[tuple[str, str], str]):
        kwargs: dict[str, object] = {"registered_forks": registered_forks}
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        return self._publisher_factory(
            self._token,
            self._account_name,
            **kwargs,
        )
