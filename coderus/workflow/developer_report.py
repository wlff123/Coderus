from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class DeveloperReportError(ValueError):
    pass


class DeveloperReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    problem_description: str
    problem_reproduction: str
    solution: str
    change_validation: str
    regression_tests: str
    remaining_issues: str

    @field_validator("*")
    @classmethod
    def validate_chinese_text(cls, value: str) -> str:
        value = value.strip()
        if not value or re.search(r"[\u4e00-\u9fff]", value) is None:
            raise ValueError("必须是非空中文说明")
        return value


def parse_developer_report(text: str) -> DeveloperReport:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeveloperReportError("开发 Agent 报告不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise DeveloperReportError("开发 Agent 报告必须是 JSON 对象")
    try:
        return DeveloperReport.model_validate(payload)
    except ValidationError as exc:
        raise DeveloperReportError(f"开发 Agent 报告不符合六段式契约：{exc}") from exc


def render_developer_report(report: DeveloperReport) -> str:
    sections = (
        ("1. 问题描述", report.problem_description),
        ("2. 问题复现", report.problem_reproduction),
        ("3. 修改方案", report.solution),
        ("4. 修改验证", report.change_validation),
        ("5. 测试回归", report.regression_tests),
        ("6. 遗留问题", report.remaining_issues),
    )
    return "\n\n".join(f"### {heading}\n{body}" for heading, body in sections)


def developer_report_schema_path() -> Path:
    return Path(__file__).with_name("developer_report.schema.json")
