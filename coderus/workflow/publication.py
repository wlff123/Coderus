"""提交封装与 PR 发布：seal、commit、publication intent 对账和 Forge 调用。

不发送任何通知；awaiting_review 持久化与飞书通知仍由 Orchestrator 负责。
"""

from __future__ import annotations

import inspect
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from coderus.forge import ForgeCapability, ForgeRegistry
from coderus.models import Task
from coderus.workflow.developer_report import DeveloperReport
from coderus.workflow.prompts import pull_request_body
from coderus.workflow.task_state import ClaimLost, cas_task_status

_PUBLISHING_EXPECTED = ("preparing", "sealing")


class TaskPublication:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        workspace_git: object,
        forges: ForgeRegistry,
        artifacts_root: Path,
        git_user_name: str,
        git_user_email: str,
        transition: Callable[[int, str, str], None],
    ) -> None:
        self.sessions = session_factory
        self.git = workspace_git
        self.forges = forges
        self.artifacts_root = artifacts_root
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.transition = transition

    async def finalize(
        self,
        task: Task,
        workspace: Path,
        branch: str,
        reports: list[DeveloperReport],
        *,
        patch_name: str,
        commit_title: str,
        claim_token: str,
    ):
        await self.git.assert_has_changes(workspace)
        self.transition(task.id, "sealing", claim_token)
        patch_path = self.artifacts_root / f"task-{task.id}" / patch_name
        sealed = await self.git.seal(workspace, patch_path)
        self._set_sealed(task.id, claim_token, sealed)
        await self.git.assert_no_secrets(workspace)
        await self.git.assert_tree(workspace, sealed.tree_sha)
        commit_sha = await self.git.commit(
            workspace, commit_title, self.git_user_name, self.git_user_email
        )
        await self.git.assert_committed_tree(workspace, commit_sha, sealed.tree_sha)
        self._set_commit(task.id, claim_token, commit_sha)
        return await self.publish(task, workspace, branch, reports, claim_token)

    async def publish_existing(
        self,
        task: Task,
        workspace: Path,
        reports: list[DeveloperReport],
        claim_token: str,
    ):
        await self.git.assert_clean_commit(workspace, task.commit_sha)
        return await self.publish(
            task, workspace, task.branch_name, reports, claim_token
        )

    async def publish(
        self,
        task: Task,
        workspace: Path,
        branch: str,
        reports: list[DeveloperReport],
        claim_token: str,
    ):
        provider = task.issue.repository.provider
        forge = self.forges.require(provider)
        if not self.forges.supports(provider, ForgeCapability.PUBLISH):
            raise ValueError("未配置代码平台发布凭据")
        publication_key = self._begin_publication(task.id, claim_token)
        body = pull_request_body(task, reports)
        body += f"\n\n<!-- coderus-publication:{publication_key} -->"
        published = await forge.publish(
            workspace=workspace,
            upstream_owner=task.issue.repository.owner,
            repository_name=task.issue.repository.name,
            default_branch=task.issue.repository.default_branch,
            branch=branch,
            title=task.issue.title[:240],
            body=body,
        )
        if inspect.isawaitable(published):
            published = await published
        return published

    def _begin_publication(self, task_id: int, claim_token: str) -> str:
        with self.sessions() as session:
            task = session.execute(
                select(Task.publication_key, Task.publication_started_at).where(
                    Task.id == task_id,
                    Task.claim_token == claim_token,
                )
            ).one_or_none()
            if task is None:
                raise ClaimLost
            publication_key = task.publication_key or secrets.token_urlsafe(32)
            started_at = task.publication_started_at or datetime.now(UTC)
            changed = cas_task_status(
                session,
                task_id,
                expected=_PUBLISHING_EXPECTED,
                new_status="publishing",
                claim_token=claim_token,
                actor="publisher",
                updates={
                    "publication_key": publication_key,
                    "publication_started_at": started_at,
                },
            )
            session.commit()
            if not changed:
                raise ClaimLost
            return publication_key

    def _set_sealed(self, task_id: int, claim_token: str, sealed: object) -> None:
        with self.sessions() as session:
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "sealing",
                    Task.claim_token == claim_token,
                )
                .values(
                    fixed_patch_path=str(sealed.patch_path),
                    reviewed_tree_sha=sealed.tree_sha,
                )
            )
            session.commit()
            if result.rowcount != 1:
                raise ClaimLost

    def _set_commit(self, task_id: int, claim_token: str, commit_sha: str) -> None:
        with self.sessions() as session:
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "sealing",
                    Task.claim_token == claim_token,
                )
                .values(commit_sha=commit_sha)
            )
            session.commit()
            if result.rowcount != 1:
                raise ClaimLost
