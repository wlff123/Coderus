from __future__ import annotations

import html
import json
import re
from urllib.parse import quote

from pydantic import ValidationError

from coderus.providers import ProviderName

from .models import ChangedRanges, ReviewOutput

MAX_REVIEW_OUTPUT_CHARS = 65_536
_FENCED_JSON_BLOCK = re.compile(r"```json[ \t]*\r?\n(?P<payload>.*?)\r?\n```", re.DOTALL)
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|])")
_PRIORITY_LABELS = {
    "P0": "P0 阻断",
    "P1": "P1 严重",
    "P2": "P2 一般",
    "P3": "P3 建议",
}


class ReviewOutputError(ValueError):
    """Raised when a review result cannot be safely published."""


def parse_review_output(stdout: str) -> ReviewOutput:
    message = _final_agent_message(stdout)
    raw_payload = message if message is not None else stdout
    if len(raw_payload) > MAX_REVIEW_OUTPUT_CHARS:
        raise ReviewOutputError("检视输出过长")
    payload = raw_payload.strip()

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        decoded = _extract_fenced_json(payload)

    try:
        return ReviewOutput.model_validate(decoded)
    except ValidationError as error:
        raise ReviewOutputError("检视结果格式无效") from error


def _final_agent_message(stdout: str) -> str | None:
    message = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            message = item["text"]
    return message


def validate_findings(output: ReviewOutput, ranges: ChangedRanges) -> ReviewOutput:
    for finding in output.findings:
        if not ranges.contains(
            finding.file_path,
            finding.line_side,
            finding.line_start,
            finding.line_end,
        ):
            raise ReviewOutputError("意见位置不在 PR 变更范围")
    return output


def render_pr_comment(
    provider: ProviderName,
    output: ReviewOutput,
    owner: str,
    name: str,
    base_sha: str,
    head_sha: str,
    review_key: str,
    *,
    changed_file_count: int | None = None,
) -> tuple[str, str]:
    hosts = {"github": "github.com", "gitcode": "gitcode.com"}
    if provider not in hosts:
        raise ValueError("unsupported PR review provider")
    marker = f"<!-- coderus-pr-review:{review_key}:{base_sha}:{head_sha} -->"
    findings = _unique_findings(output)
    summary = (
        f"发现 {len(findings)} 项需要处理的问题"
        if findings
        else "未发现需要反馈的具体问题"
    )
    sections = [
        "## Coderus 代码检视",
        f"- 检视版本：{_code_span(head_sha)}",
    ]
    if changed_file_count is not None:
        sections.append(f"- 检视输入：{changed_file_count} 个变更文件")
    sections.append("- PR 修改摘要：")
    sections.extend(
        f"  {index}. {_escape_markdown(sentence)}"
        for index, sentence in enumerate(output.change_summary, start=1)
    )
    sections.append(f"- 结论：{summary}")

    for index, finding in enumerate(findings, start=1):
        sha = base_sha if finding.line_side == "LEFT" else head_sha
        version_label = "原版本" if finding.line_side == "LEFT" else "新版本"
        path = quote(finding.file_path, safe="/")
        url = (
            f"https://{hosts[provider]}/{quote(owner, safe='')}/{quote(name, safe='')}/blob/"
            f"{quote(sha, safe='')}/{path}#L{finding.line_start}-L{finding.line_end}"
        )
        sections.extend(
            [
                "",
                (
                    f"### {index}. [{_PRIORITY_LABELS[finding.priority]}] "
                    f"{_escape_markdown(finding.title)}"
                ),
                "",
                f"- 文件：{_code_span(finding.file_path)}",
                (
                    f"- 代码范围：[{version_label}第 {finding.line_start}-{finding.line_end} 行]"
                    f"({url})"
                ),
                f"- 问题说明：{_escape_markdown(finding.problem)}",
                f"- 影响分析：{_escape_markdown(finding.impact)}",
                f"- 修改建议：{_escape_markdown(finding.suggestion)}",
            ]
        )

    sections.extend(["", "> 本次为静态代码检视，未执行项目测试。", marker])
    return "\n".join(sections), marker


def _extract_fenced_json(stdout: str) -> object:
    matches = list(_FENCED_JSON_BLOCK.finditer(stdout))
    if len(matches) != 1:
        raise ReviewOutputError("检视输出必须是完整 JSON 或单一 JSON 代码块")

    match = matches[0]
    before = stdout[: match.start()]
    after = stdout[match.end() :]
    if "```" in before or after.strip():
        raise ReviewOutputError("检视输出必须是完整 JSON 或单一 JSON 代码块")
    try:
        return json.loads(match.group("payload"))
    except json.JSONDecodeError as error:
        raise ReviewOutputError("检视结果不是有效 JSON") from error


def _unique_findings(output: ReviewOutput) -> list:
    seen: set[tuple[str, str, int, int]] = set()
    findings = []
    for finding in output.findings:
        location = (
            finding.file_path,
            finding.line_side,
            finding.line_start,
            finding.line_end,
        )
        if location not in seen:
            seen.add(location)
            findings.append(finding)
    return findings


def _escape_markdown(value: str) -> str:
    value = " ".join(value.splitlines())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", html.escape(value, quote=False))


def _code_span(value: str) -> str:
    longest_backtick_run = max((len(run) for run in re.findall(r"`+", value)), default=0)
    delimiter = "`" * (longest_backtick_run + 1)
    return f"{delimiter}{html.escape(value, quote=False)}{delimiter}"
