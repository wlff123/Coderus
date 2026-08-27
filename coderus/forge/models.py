"""平台适配层共享数据模型：平台名、仓库与 Issue 快照、PR 发布结果。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ProviderName = Literal["github", "gitcode"]


@dataclass(frozen=True, slots=True)
class ForkResult:
    url: str
    owner: str
    created: bool


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class PublishResult:
    url: str
    number: int
    state: Literal["open", "closed"]
    fork_url: str
    branch: str
    pr_created: bool


@dataclass(frozen=True, slots=True)
class PullRequestDetails:
    number: int
    url: str
    state: Literal["open", "closed"]
    merged: bool
    base_sha: str
    head_sha: str
    base_ref: str
    head_ref: str
    head_repository_url: str


@dataclass(frozen=True, slots=True)
class PRCommentResult:
    url: str
    created: bool


@dataclass(frozen=True, slots=True)
class PRFeedbackItem:
    provider_id: str
    kind: Literal["issue_comment", "review", "review_comment"]
    author: str
    author_association: str
    body: str
    url: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class Repository:
    provider: ProviderName
    owner: str
    name: str
    canonical_url: str
    default_branch: str | None = None
    is_private: bool | None = None
    issues_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class Issue:
    repository: Repository
    external_id: str
    number: int
    title: str
    body: str | None
    state: Literal["open", "closed"]
    labels: tuple[str, ...]
    canonical_url: str
    created_at: datetime | None
    updated_at: datetime | None
