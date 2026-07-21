from dataclasses import dataclass
from typing import Literal


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
