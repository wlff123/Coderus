"""仓库管理用例：添加、同步与启停，事务边界在此收敛。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from coderus.application.errors import CommandError, Conflict, Forbidden, NotFound
from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.forge.urls import parse_repository_url
from coderus.issues.service import sync_repository
from coderus.models import Repository, User


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    id: int
    owner: str
    name: str
    is_enabled: bool


class SyncFailed(CommandError):
    """仓库同步失败，消息为已持久化的用户可见错误文案。"""


class RepositoryCommands:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        providers: Mapping[str, object],
        forges: ForgeRegistry,
        error_formatter: Callable[[BaseException], str] = str,
    ) -> None:
        self._sessions = session_factory
        self._providers = providers
        self._forges = forges
        self._format_error = error_formatter

    async def add(self, url: str, actor_id: int) -> RepositoryRef:
        """校验仓库元数据并注册仓库，必要时确保 Fork 就绪。"""
        with self._sessions() as session:
            actor = session.get(User, actor_id)
            if actor is None or actor.role != "admin":
                raise Forbidden("没有权限添加仓库")
        parsed = parse_repository_url(url)
        provider = self._providers[parsed.provider]
        metadata = await asyncio.to_thread(
            provider.get_repository, parsed.canonical_url
        )
        if metadata.is_private or metadata.issues_enabled is False:
            raise ValueError("仓库必须公开且启用 Issue")
        fork = None
        if self._forges.supports(metadata.provider, ForgeCapability.ENSURE_FORK):
            forge = self._forges.get(metadata.provider)
            fork = await forge.ensure_fork(metadata.owner, metadata.name)
        with self._sessions() as session:
            repository = Repository(
                provider=metadata.provider,
                owner=metadata.owner,
                name=metadata.name,
                canonical_url=metadata.canonical_url,
                default_branch=metadata.default_branch or "main",
                fork_owner=fork.owner if fork else None,
                fork_url=fork.url if fork else None,
                created_by=actor_id,
            )
            session.add(repository)
            session.commit()
            return _ref(repository)

    def sync(self, repository_id: int) -> RepositoryRef:
        """同步单个仓库；失败时记录失败状态并抛出 SyncFailed。"""
        with self._sessions() as session:
            repository = session.get(Repository, repository_id)
            if repository is None:
                raise NotFound("仓库不存在")
            if repository.sync_status == "running":
                raise Conflict("仓库正在同步，请稍后刷新状态")
            try:
                sync_repository(
                    session, repository, self._providers[repository.provider]
                )
                session.commit()
                return _ref(repository)
            except Exception as exc:
                repository.sync_status = "failed"
                repository.last_sync_error = self._format_error(exc)[:1000]
                message = repository.last_sync_error
                session.commit()
                raise SyncFailed(message) from exc

    def toggle(self, repository_id: int) -> RepositoryRef:
        """启用或停用仓库。"""
        with self._sessions() as session:
            repository = session.get(Repository, repository_id)
            if repository is None:
                raise NotFound("仓库不存在")
            if repository.sync_status == "running":
                raise Conflict("仓库正在同步，当前不能修改启用状态")
            repository.is_enabled = not repository.is_enabled
            ref = _ref(repository)
            session.commit()
            return ref


def _ref(repository: Repository) -> RepositoryRef:
    return RepositoryRef(
        id=repository.id,
        owner=repository.owner,
        name=repository.name,
        is_enabled=repository.is_enabled,
    )
