from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:/")


def normalize_repository_path(file_path: str) -> str | None:
    """Return a canonical repository-relative path, or None when unsafe."""
    path = file_path.replace("\\", "/")
    if not path or path.startswith("/") or _WINDOWS_ABSOLUTE_PATH.match(path):
        return None
    if any(
        (ord(character) < 32 and character != "\t") or ord(character) == 127 for character in path
    ):
        return None
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ChangedRanges:
    ranges: Mapping[tuple[str, str], tuple[tuple[int, int], ...]]
    comparison_sha: str | None = None
    changed_file_count: int = 0
    additions: int = 0
    deletions: int = 0

    def contains(self, file_path: str, side: str, start: int, end: int) -> bool:
        return self.clip(file_path, side, start, end) == (start, end)

    def clip(self, file_path: str, side: str, start: int, end: int) -> tuple[int, int] | None:
        path = normalize_repository_path(file_path)
        if path is None or side not in {"LEFT", "RIGHT"} or start < 1 or end < start:
            return None
        overlaps = tuple(
            dict.fromkeys(
                (max(start, range_start), min(end, range_end))
                for range_start, range_end in self.ranges.get((path, side), ())
                if max(start, range_start) <= min(end, range_end)
            )
        )
        if len(overlaps) != 1:
            return None
        clipped_start, clipped_end = overlaps[0]
        context_lines = clipped_start - start + end - clipped_end
        return overlaps[0] if context_lines <= 1 else None


@dataclass(frozen=True, slots=True)
class ReviewInput:
    ranges: ChangedRanges
    unified_diff: str
    review_base: str


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    priority: Literal["P0", "P1", "P2", "P3"]
    title: str = Field(min_length=1, max_length=200)
    file_path: str = Field(min_length=1, max_length=512)
    line_side: Literal["LEFT", "RIGHT"]
    line_start: int = Field(gt=0)
    line_end: int = Field(gt=0)
    problem: str = Field(min_length=1, max_length=2_000)
    impact: str = Field(min_length=1, max_length=2_000)
    suggestion: str = Field(min_length=1, max_length=2_000)

    @field_validator("title", "problem", "impact", "suggestion")
    @classmethod
    def require_chinese_text(cls, value: str) -> str:
        if not value.strip() or not any("\u4e00" <= character <= "\u9fff" for character in value):
            raise ValueError("字段必须包含中文内容")
        return value

    @field_validator("file_path")
    @classmethod
    def require_repository_path(cls, value: str) -> str:
        normalized = normalize_repository_path(value)
        if normalized is None:
            raise ValueError("文件路径必须是安全的仓库相对路径")
        return normalized

    @model_validator(mode="after")
    def require_ordered_lines(self) -> ReviewFinding:
        if self.line_end < self.line_start:
            raise ValueError("结束行不能小于起始行")
        return self


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    _comparison_sha: str | None = PrivateAttr(default=None)

    change_summary: list[str] = Field(min_length=1, max_length=5)
    findings: list[ReviewFinding]

    @field_validator("change_summary")
    @classmethod
    def require_chinese_summary_sentences(cls, value: list[str]) -> list[str]:
        for sentence in value:
            if (
                sentence != sentence.strip()
                or not sentence
                or len(sentence) > 300
                or "\n" in sentence
                or "\r" in sentence
                or not any("\u4e00" <= character <= "\u9fff" for character in sentence)
            ):
                raise ValueError("修改摘要必须是简洁的中文单句")
        return value
