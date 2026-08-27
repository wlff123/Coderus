from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coderus.forge.models import Issue as ProviderIssue
from coderus.forge.models import Repository as ProviderRepository
from coderus.forge.urls import parse_issue_url
from coderus.models import Issue, Repository, Task, User
from coderus.tasks.statuses import TERMINAL_TASK_STATES


class IssueProvider(Protocol):
    def list_open_issues(self, repository: ProviderRepository) -> list[ProviderIssue]: ...

    def get_issue(self, repository: ProviderRepository, number: int) -> ProviderIssue: ...


def provider_repository(repository: Repository) -> ProviderRepository:
    return ProviderRepository(
        provider=repository.provider,  # type: ignore[arg-type]
        owner=repository.owner,
        name=repository.name,
        canonical_url=repository.canonical_url,
        default_branch=repository.default_branch,
        is_private=False,
        issues_enabled=True,
    )


def upsert_provider_issue(session: Session, repository: Repository, source: ProviderIssue) -> Issue:
    issue = session.scalar(
        select(Issue).where(
            Issue.repository_id == repository.id,
            Issue.number == source.number,
        )
    )
    if issue is None:
        issue = Issue(repository=repository, external_id=source.external_id, number=source.number)
        session.add(issue)
    issue.external_id = source.external_id
    issue.title = source.title
    issue.body = source.body or ""
    issue.labels = list(source.labels)
    issue.state = source.state
    issue.source_url = source.canonical_url
    issue.source_updated_at = source.updated_at
    session.flush()
    return issue


def add_provider_issue(
    session: Session,
    providers: Mapping[str, IssueProvider],
    issue_url: str,
) -> Issue:
    source_repository, number = parse_issue_url(issue_url)
    repository = session.scalar(
        select(Repository).where(
            Repository.provider == source_repository.provider,
            Repository.owner == source_repository.owner,
            Repository.name == source_repository.name,
            Repository.is_enabled.is_(True),
        )
    )
    if repository is None:
        raise ValueError("该 Issue 所属仓库未由管理员授权")
    provider = providers.get(repository.provider)
    if provider is None:
        raise ValueError("该 Issue 所属代码托管服务未配置")
    source = provider.get_issue(source_repository, number)
    return upsert_provider_issue(session, repository, source)


def add_and_dispatch_issue(
    session: Session,
    providers: Mapping[str, IssueProvider],
    issue_url: str,
    creator: User,
    *,
    commit: bool = True,
) -> Task:
    issue = add_provider_issue(session, providers, issue_url)
    return dispatch_issue(session, issue, creator, commit=commit)


def sync_repository(
    session: Session,
    repository: Repository,
    provider: IssueProvider,
    *,
    full: bool = True,
) -> int:
    if repository.sync_status == "running":
        return 0
    repository.sync_status = "running"
    sync_started_at = datetime.now(UTC)
    repository.sync_started_at = sync_started_at
    repository.last_sync_error = None
    session.commit()
    try:
        source_repository = provider_repository(repository)
        if hasattr(provider, "list_issues"):
            if full:
                sources = provider.list_issues(source_repository, state="all")
            else:
                cursor = repository.sync_cursor_updated_at
                if cursor is not None and cursor.tzinfo is None:
                    cursor = cursor.replace(tzinfo=UTC)
                sources = provider.list_issues(
                    source_repository,
                    state="all",
                    updated_since=cursor,
                )
        else:
            sources = provider.list_open_issues(source_repository)
        for source in sources:
            upsert_provider_issue(session, repository, source)
    except Exception as exc:
        repository.sync_status = "failed"
        repository.last_sync_error = str(exc)[:1000]
        session.flush()
        raise
    repository.sync_status = "succeeded"
    repository.last_synced_at = datetime.now(UTC)
    repository.sync_cursor_updated_at = sync_started_at
    session.flush()
    return len(sources)


def dispatch_issue(
    session: Session,
    issue: Issue,
    creator: User,
    instructions: str = "",
    *,
    commit: bool = True,
) -> Task:
    if issue.state != "open" or issue.triage_state != "discovered":
        raise ValueError("只有待处理的开放 Issue 可以派发")
    existing = session.scalar(
        select(Task).where(
            Task.issue_id == issue.id,
            Task.status.not_in(TERMINAL_TASK_STATES),
        )
    )
    if existing is not None:
        raise ValueError("该 Issue 已有未结束任务")
    if not issue.repository.is_enabled:
        raise ValueError("该仓库已停用，不能派发新任务")
    if issue.triage_state == "ignored":
        raise ValueError("已忽略的 Issue 不能派发")
    task = Task(issue=issue, creator=creator, status="queued", instructions=instructions.strip())
    issue.triage_state = "dispatched"
    session.add(task)
    try:
        if commit:
            session.commit()
        else:
            session.flush()
    except IntegrityError:
        session.rollback()
        raise ValueError("该 Issue 已有未结束任务") from None
    return task
