from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .instructions import ReviewAxis
from .models import FindingRecheck, ReviewFinding, ReviewOutput

REVIEW_HISTORY_VERSION = 1
_SHA = re.compile(r"[0-9a-f]{40}")


class TrackedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^RV-[0-9]+-[0-9]+$")
    axis: ReviewAxis
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    comparison_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    finding: ReviewFinding

    def prompt_data(self) -> dict:
        result = self.model_dump(mode="json")
        for name in ("problem", "impact", "suggestion"):
            result["finding"][name] = result["finding"][name][:700]
        return result

    def matches(self, other: ReviewFinding) -> bool:
        old = self.finding
        if (old.file_path, old.line_side) != (other.file_path, other.line_side):
            return False
        return (old.title, old.problem) == (other.title, other.problem)


@dataclass(frozen=True, slots=True)
class PreviousReview:
    task_id: int
    head_sha: str
    comment_url: str
    result: dict
    scope: str | None
    open_findings: tuple[TrackedFinding, ...]

    @classmethod
    def load(
        cls, task_id: int, head_sha: str | None, comment_url: str | None, result: dict | None,
    ) -> PreviousReview | None:
        if not head_sha or _SHA.fullmatch(head_sha) is None or not comment_url:
            return None
        history = result.get("review_history") if isinstance(result, dict) else None
        if not isinstance(history, dict) or history.get("version") != REVIEW_HISTORY_VERSION:
            return None
        raw_items = history.get("open_findings")
        if not isinstance(raw_items, list):
            return None
        try:
            items = tuple(TrackedFinding.model_validate(item) for item in raw_items)
            ReviewOutput(change_summary=result.get("change_summary"), findings=[])
        except ValidationError:
            return None
        if len({item.id for item in items}) != len(items):
            return None
        scope = history.get("scope")
        reportable = tuple(item for item in items if item.finding.priority != "P3")
        return cls(task_id, head_sha, comment_url, result, scope, reportable)

    def for_axis(self, axis: ReviewAxis) -> tuple[TrackedFinding, ...]:
        return tuple(item for item in self.open_findings if item.axis == axis)

    @property
    def comment_task_id(self) -> int:
        source = self.result["review_history"].get("reused_from_task_id")
        return source if type(source) is int and source > 0 else self.task_id

    def reuse_result(self) -> dict:
        return {
            "change_summary": self.result["change_summary"],
            "findings": [],
            "unlocated_findings": [],
            "review_history": {
                **self.result["review_history"],
                "mode": "reused",
                "reused_from_task_id": self.comment_task_id,
            },
        }


def scope_digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def apply_rechecks(
    prior: tuple[TrackedFinding, ...],
    rechecks: list[FindingRecheck],
) -> list[tuple[TrackedFinding, FindingRecheck]]:
    by_id = {check.id: check for check in rechecks}
    return [
        (
            item,
            by_id.get(item.id) or FindingRecheck(
                id=item.id,
                status="needs_confirmation",
                reason="本轮缺少可核实的规格，无法复核。",
            ),
        )
        for item in prior
    ]
