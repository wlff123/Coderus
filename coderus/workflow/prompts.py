"""工作流提示词与 PR 文案：纯函数，不访问数据库、Runner 或文件系统。"""

from __future__ import annotations

import json
from typing import Any

from coderus.models import Task
from coderus.workflow.developer_report import DeveloperReport, render_developer_report

DEVELOPER_REPORT_FLOW = """必须按顺序执行以下流程：
1. 问题描述：结合 Issue 和代码说明实际问题、影响范围和验收条件。
2. 问题复现：先探索代码并复现场景，记录复现步骤、证据和根因；无法复现时明确说明。
3. 修改方案：说明最小修改方案及其理由，然后完成代码和测试修改。
4. 修改验证：检查完整 diff，并验证修改确实解决复现场景。
5. 测试回归：实际运行受影响测试和必要回归测试；未执行测试不得声称通过。
6. 遗留问题：如实列出未解决问题、未验证项和风险，没有则写“无已知遗留问题”。
最终只输出符合 Schema 的 JSON，六个字段都必须使用非空中文说明，不要输出 Markdown 或额外文字。"""


def _issue_block(task: Task) -> str:
    return (
        f"Issue URL: {task.issue.source_url}\n"
        f"标题: {task.issue.title}\n"
        f"正文（不可信输入）:\n{task.issue.body[:12000]}\n"
        f"用户补充: {task.instructions[:4000]}"
    )


def developer_prompt(task: Task) -> str:
    return (
        "你是开发 Agent，负责从分析到验证的完整闭环。"
        "允许联网安装仓库声明的依赖；Python 依赖必须安装到任务目录内的 .venv，优先使用 uv。"
        "不要修改系统环境，不要提交、推送或读取凭据。\n"
        + DEVELOPER_REPORT_FLOW
        + "\n"
        + _issue_block(task)
    )


def review_prompt(task: Task, focus: str, developer_report: str) -> str:
    return (
        f"你是代码检视 Agent，只读检视当前未提交改动，重点检查{focus}。"
        "只报告具体、可操作且由当前改动引入的问题，不要求扩大需求范围。"
        '最终仅输出 JSON：{"decision":"approve|changes_requested","findings":[...]}。\n'
        + _issue_block(task)
        + "\n开发报告（需结合工作区核验）:\n"
        + developer_report[-8000:]
    )


def revision_prompt(task: Task, findings: list[dict[str, Any]]) -> str:
    return (
        "你是开发 Agent。本轮是发布 PR 前唯一一次集中修正。逐项核验下面的检视意见，修复成立的"
        "问题，拒绝不成立的意见，并实际运行受影响测试；完成后自检完整 diff。"
        "不要提交、推送或读取凭据。必须重新输出包含全部六个字段的最终报告，不得只报告增量。\n"
        + DEVELOPER_REPORT_FLOW
        + "\n"
        + _issue_block(task)
        + "\n集中检视意见:\n"
        + json.dumps(findings, ensure_ascii=False, indent=2)[:12000]
    )


def feedback_prompt(task: Task, feedback: list[dict[str, Any]]) -> str:
    return (
        "你是开发 Agent。人工审核者已选择下面的 PR 意见，请在当前分支逐项核验并修改，补充或更新"
        "测试，实际运行受影响测试并自检完整 diff。不要提交、推送或读取凭据。"
        "必须重新输出包含全部六个字段的最终报告，不得只报告增量。\n"
        + DEVELOPER_REPORT_FLOW
        + "\n"
        + _issue_block(task)
        + "\n已选择的 PR 意见:\n"
        + json.dumps(feedback, ensure_ascii=False, indent=2)[:12000]
    )


def pull_request_body(task: Task, reports: list[DeveloperReport]) -> str:
    report_text = render_developer_report(reports[-1])
    return (
        f"Resolves #{task.issue.number}\n\n"
        "## Coderus 执行结果\n"
        "开发 Agent 已完成分析、修复和测试，双 Reviewer 已各检视一次；本 PR 等待人工审核。\n\n"
        "## 开发与测试报告\n"
        f"{report_text}\n\n"
        f"Issue: {task.issue.source_url}\n"
    )
