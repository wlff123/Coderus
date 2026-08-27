"""评测基线的版本化数据契约。

只保存平台、``owner/name``、Issue 编号和指标，不含凭据、路径、
Issue 正文或 Agent 输出。人工注解只承载核实过的事实，未知一律为 None。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EVALUATION_CONTRACT_VERSION = 1

TaskOutcome = Literal[
    "pr_created",
    "manual_intervention",
    "failed",
    "cancelled",
    "closed",
    "incomplete",
]


class TaskAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_key: str
    tests_passed: bool | None = None
    accepted_without_code_changes: bool | None = None
    human_changed_lines: int | None = Field(default=None, ge=0)


class BaselineSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[1] = EVALUATION_CONTRACT_VERSION
    task_keys: tuple[str, ...] = Field(min_length=10, max_length=20)
    annotations: tuple[TaskAnnotation, ...] = ()

    @field_validator("task_keys")
    @classmethod
    def validate_task_keys(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("task keys must be unique")
        if any(
            not value.startswith("RE-") or not value[3:].isdigit() for value in values
        ):
            raise ValueError("task keys must use RE-N format")
        return values

    @model_validator(mode="after")
    def validate_annotations(self) -> BaselineSelection:
        annotation_keys = tuple(item.task_key for item in self.annotations)
        if len(set(annotation_keys)) != len(annotation_keys):
            raise ValueError("annotation task keys must be unique")
        if not set(annotation_keys).issubset(self.task_keys):
            raise ValueError("annotations must reference selected tasks")
        return self


class TaskBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_key: str
    provider: Literal["github", "gitcode"]
    repository: str
    issue_number: int
    status: str
    outcome: TaskOutcome
    duration_seconds: float | None
    transition_count: int
    developer_runs: int
    reviewer_runs: int
    reviewer_findings: int
    model_requests: int | None
    model_output_bytes: int | None
    tests_passed: bool | None
    accepted_without_code_changes: bool | None
    human_changed_lines: int | None


class BaselineSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    pr_created: int
    manual_intervention: int
    failed: int
    cancelled: int
    closed: int
    incomplete: int
    pr_created_rate: float
    median_duration_seconds: float | None
    verified_test_pass_rate: float | None
    accepted_without_code_changes_rate: float | None
    median_human_changed_lines: float | None


class BaselineReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[1] = EVALUATION_CONTRACT_VERSION
    generated_at: datetime
    records: tuple[TaskBaseline, ...]
    summary: BaselineSummary
