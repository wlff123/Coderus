from __future__ import annotations

import json

import pytest

from coderus.workflow.developer_report import (
    DeveloperReportError,
    developer_report_schema_path,
    parse_developer_report,
    render_developer_report,
)

VALID_REPORT = {
    "problem_description": "提交归档后，旧消息仍可能被重新识别为活跃消息。",
    "problem_reproduction": "已通过模拟持久化空写成功但文件未清空的场景复现问题。",
    "solution": "写入后重新读取消息文件，并与预期保留消息进行结构化比较。",
    "change_validation": "直接验证持久化不一致时提交失败且不会写入完成标记。",
    "regression_tests": "新增两个回归用例；执行目标测试，结果全部通过。",
    "remaining_issues": "无。",
}


def test_developer_report_requires_six_non_empty_chinese_fields() -> None:
    report = parse_developer_report(json.dumps(VALID_REPORT, ensure_ascii=False))

    assert report.remaining_issues == "无。"
    for key in VALID_REPORT:
        invalid = VALID_REPORT | {key: "pytest -q"}
        with pytest.raises(DeveloperReportError, match=key):
            parse_developer_report(json.dumps(invalid, ensure_ascii=False))


@pytest.mark.parametrize("payload", ["not-json", "[]", "{}"])
def test_developer_report_rejects_invalid_payload(payload: str) -> None:
    with pytest.raises(DeveloperReportError):
        parse_developer_report(payload)


def test_developer_report_renders_fixed_chinese_sections() -> None:
    report = parse_developer_report(json.dumps(VALID_REPORT, ensure_ascii=False))

    rendered = render_developer_report(report)

    headings = [
        "### 1. 问题描述",
        "### 2. 问题复现",
        "### 3. 修改方案",
        "### 4. 修改验证",
        "### 5. 测试回归",
        "### 6. 遗留问题",
    ]
    assert all(heading in rendered for heading in headings)
    assert [rendered.index(heading) for heading in headings] == sorted(
        rendered.index(heading) for heading in headings
    )


def test_developer_report_schema_matches_model_contract() -> None:
    schema = json.loads(developer_report_schema_path().read_text(encoding="utf-8"))

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == list(VALID_REPORT)
    assert set(schema["properties"]) == set(VALID_REPORT)
    assert all(field["minLength"] == 1 for field in schema["properties"].values())
