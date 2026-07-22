from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

REVIEWER_CONTRACT_VERSION = 1


class ReviewerResultError(ValueError):
    pass


class ReviewerFinding(BaseModel):
    model_config = ConfigDict(extra="allow")

    severity: Literal["critical", "high", "medium", "low"]
    message: str = Field(min_length=1, max_length=4_000)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        return value.strip()


class ReviewerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal[1] = REVIEWER_CONTRACT_VERSION
    decision: Literal["approve", "changes_requested"]
    findings: list[ReviewerFinding]

    @model_validator(mode="after")
    def validate_decision_findings(self) -> ReviewerResult:
        if self.decision == "approve" and self.findings:
            raise ValueError("approve must not contain findings")
        if self.decision == "changes_requested" and not self.findings:
            raise ValueError("changes_requested must contain at least one finding")
        return self


def parse_reviewer_result(text: str) -> ReviewerResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewerResultError("检视结果不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ReviewerResultError("检视结果必须是 JSON 对象")
    try:
        return ReviewerResult.model_validate(payload)
    except ValidationError as exc:
        raise ReviewerResultError("检视结果不符合版本化契约") from exc


def reviewer_result_schema_path() -> Path:
    return Path(__file__).with_name("reviewer_result.schema.json")
