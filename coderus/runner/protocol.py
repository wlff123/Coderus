from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class RetryableAgentError(RuntimeError):
    """An Agent startup failure known to be transient and free of side effects."""


class Stage(StrEnum):
    DEVELOP = "develop"
    REVIEW_CORRECTNESS = "review_correctness"
    REVIEW_SECURITY = "review_security"
    PR_REVIEW = "pr_review"
    REVISE = "revise"


class AgentRole(StrEnum):
    DEVELOPER = "developer"
    REVIEWER_A = "reviewer_a"
    REVIEWER_B = "reviewer_b"
    PR_REVIEWER = "pr_reviewer"

    @property
    def read_only(self) -> bool:
        return self is not AgentRole.DEVELOPER


_STAGE_ROLES = {
    Stage.DEVELOP: AgentRole.DEVELOPER,
    Stage.REVIEW_CORRECTNESS: AgentRole.REVIEWER_A,
    Stage.REVIEW_SECURITY: AgentRole.REVIEWER_B,
    Stage.PR_REVIEW: AgentRole.PR_REVIEWER,
    Stage.REVISE: AgentRole.DEVELOPER,
}


class JobStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class JobSpec:
    job_id: str
    stage: Stage
    role: AgentRole
    workspace: Path
    prompt: str
    timeout_seconds: float = 3600
    max_output_bytes: int = 1_000_000
    output_schema: Path | None = None
    session_id: str | None = None
    review_base: str | None = None
    proxy_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id must not be empty")
        if _STAGE_ROLES[self.stage] is not self.role:
            raise ValueError(f"role {self.role} does not match stage {self.stage}")
        if self.stage is Stage.PR_REVIEW:
            if not self.review_base:
                raise ValueError("review_base is required for PR review jobs")
            if self.session_id is not None:
                raise ValueError("session_id is not supported for PR review jobs")
        elif self.review_base is not None:
            raise ValueError("review_base is only supported for PR review jobs")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: str
    status: JobStatus
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    duration_seconds: float
