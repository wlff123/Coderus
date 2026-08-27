from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from coderus.forge.models import (
    ForkResult,
    PRCommentResult,
    PRFeedbackItem,
    PublishResult,
    PullRequestDetails,
)


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """发布 PR 所需的全部输入；构造即校验，杜绝字典键漂移。"""

    workspace: Path
    upstream_owner: str
    repository_name: str
    default_branch: str
    branch: str
    title: str
    body: str

    def __post_init__(self) -> None:
        workspace = Path(self.workspace)
        if not workspace.is_absolute():
            raise ValueError("workspace must be an absolute path")
        object.__setattr__(self, "workspace", workspace.resolve())
        for field_name in ("upstream_owner", "repository_name", "title"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")
        if not self.default_branch.strip() or not self.branch.strip():
            raise ValueError("branch names must not be empty")
        if self.default_branch == self.branch:
            raise ValueError("work branch must differ from the default branch")


class Forge(Protocol):
    async def ensure_fork(self, owner: str, name: str) -> ForkResult: ...

    async def publish(self, request: PublishRequest) -> PublishResult: ...

    async def list_pr_feedback(
        self, owner: str, name: str, pr_number: int
    ) -> list[PRFeedbackItem]: ...

    async def get_pr_status(self, owner: str, name: str, pr_number: int) -> str: ...

    async def get_pull_request(
        self, owner: str, name: str, pr_number: int
    ) -> PullRequestDetails: ...

    async def publish_pr_comment(
        self, owner: str, name: str, pr_number: int, body: str, marker: str
    ) -> PRCommentResult: ...
