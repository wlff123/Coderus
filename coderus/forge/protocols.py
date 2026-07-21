from __future__ import annotations

from typing import Any, Protocol

from coderus.publisher import (
    ForkResult,
    PRCommentResult,
    PRFeedbackItem,
    PublishResult,
    PullRequestDetails,
)


class Forge(Protocol):
    async def ensure_fork(self, owner: str, name: str) -> ForkResult: ...

    async def publish(self, **kwargs: Any) -> PublishResult: ...

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
