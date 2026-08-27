"""提示词特征测试：锁定流程要求、注入防护声明和关键上下文不丢失。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coderus.workflow.developer_report import DeveloperReport
from coderus.workflow.prompts import (
    developer_prompt,
    feedback_prompt,
    pull_request_body,
    review_prompt,
    revision_prompt,
)

REPORT_SECTIONS = ("问题描述", "问题复现", "修改方案", "修改验证", "测试回归", "遗留问题")


@pytest.fixture
def task() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        instructions="优先验证回归场景",
        issue=SimpleNamespace(
            number=42,
            title="crash on start",
            body="启动时崩溃的详细描述",
            source_url="https://github.com/octo/demo/issues/42",
        ),
    )


def report() -> DeveloperReport:
    return DeveloperReport(
        problem_description="启动崩溃",
        problem_reproduction="已复现",
        solution="修复空指针",
        change_validation="diff 已检查",
        regression_tests="测试已运行",
        remaining_issues="无已知遗留问题",
    )


def test_developer_prompt_keeps_flow_and_issue_context(task) -> None:
    prompt = developer_prompt(task)

    assert "https://github.com/octo/demo/issues/42" in prompt
    assert "优先验证回归场景" in prompt
    assert "不可信输入" in prompt
    assert "不要提交、推送或读取凭据" in prompt
    assert "符合 Schema 的 JSON" in prompt
    for section in REPORT_SECTIONS:
        assert section in prompt


def test_review_prompt_is_read_only_and_embeds_report(task) -> None:
    prompt = review_prompt(task, "正确性、回归风险和测试覆盖", "开发报告全文")

    assert "只读检视" in prompt
    assert "正确性、回归风险和测试覆盖" in prompt
    assert '"decision":"approve|changes_requested"' in prompt
    assert "开发报告全文" in prompt
    assert "https://github.com/octo/demo/issues/42" in prompt


def test_revision_prompt_requires_full_report_and_findings(task) -> None:
    findings = [{"severity": "high", "message": "缺少空值检查"}]
    prompt = revision_prompt(task, findings)

    assert "缺少空值检查" in prompt
    assert "不得只报告增量" in prompt
    for section in REPORT_SECTIONS:
        assert section in prompt


def test_feedback_prompt_embeds_selected_feedback(task) -> None:
    feedback = [{"author": "octocat", "body": "请补充测试"}]
    prompt = feedback_prompt(task, feedback)

    assert "请补充测试" in prompt
    assert "不得只报告增量" in prompt
    assert "https://github.com/octo/demo/issues/42" in prompt


def test_pull_request_body_links_issue_and_renders_report(task) -> None:
    body = pull_request_body(task, [report()])

    assert body.startswith("Resolves #42")
    assert "Coderus 执行结果" in body
    assert "修复空指针" in body
    assert "Issue: https://github.com/octo/demo/issues/42" in body
