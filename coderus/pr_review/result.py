from __future__ import annotations

import html
import json
import re
import shlex
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import ValidationError

from coderus.forge import ProviderName
from coderus.tasks.error_codes import TaskErrorCode

from .history import PreviousReview, TrackedFinding
from .models import ChangedRanges, FindingRecheck, ReviewFinding, ReviewOutput

MAX_REVIEW_OUTPUT_CHARS = 65_536
_SAFE_DIFF_OPTIONS = {
    "--find-copies",
    "--find-renames",
    "--histogram",
    "--minimal",
    "--no-color",
    "--no-ext-diff",
    "--patience",
}
_SAFE_DIFF_OPTION_VALUE = re.compile(
    r"(?:-U\d+|--unified=\d+|--find-(?:copies|renames)=\d+%?)"
)
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|])")
_PRIORITY_LABELS = {
    "P0": "P0 阻断",
    "P1": "P1 严重",
    "P2": "P2 一般",
    "P3": "P3 建议",
}


class ReviewOutputError(ValueError):
    """Raised when a review result cannot be safely published."""

    error_code = TaskErrorCode.AGENT_OUTPUT_INVALID


def review_output_schema_path() -> Path:
    return Path(__file__).with_name("review_output.schema.json")


def parse_review_output(
    stdout: str, *, comparison_sha: str, incremental: bool = False,
    previous_ids: tuple[str, ...] = (),
) -> ReviewOutput:
    message = None
    inspected = False
    completed = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.failed":
            raise ReviewOutputError("Codex 检视失败")
        if kind == "turn.completed":
            completed = True
        item = event.get("item")
        if kind != "item.completed" or not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            message = item["text"]
        if (
            item.get("type") == "command_execution"
            and item.get("status") == "completed"
            and item.get("exit_code") == 0
            and isinstance(item.get("command"), str)
            and _is_fixed_base_git_diff(item["command"], comparison_sha, incremental)
        ):
            inspected = True

    if not completed or message is None:
        raise ReviewOutputError("Codex 未返回完整检视事件")
    if not inspected:
        raise ReviewOutputError("Codex 未成功检查完整 Git diff")
    if len(message) > MAX_REVIEW_OUTPUT_CHARS:
        raise ReviewOutputError("检视输出过长")
    try:
        decoded = json.loads(message)
        if not isinstance(decoded, dict):
            raise ValueError
    except ValueError as error:
        raise ReviewOutputError("检视结果不是有效 JSON") from error
    if decoded.pop("review_completed", None) is not True:
        raise ReviewOutputError("Codex 未确认检视完成")
    try:
        entries = decoded.pop("rechecks", [])
        if not isinstance(entries, list):
            raise ReviewOutputError("历史意见复核格式无效")
        rechecks = [FindingRecheck.model_validate(item) for item in entries]
        findings = decoded.get("findings")
        if isinstance(findings, list):
            decoded["findings"] = [
                item for item in findings
                if not (isinstance(item, dict) and item.get("priority") == "P3")
            ]
        output = ReviewOutput.model_validate(decoded)
    except ValidationError as error:
        raise ReviewOutputError("检视结果格式无效") from error
    if sorted(entry.id for entry in rechecks) != sorted(previous_ids):
        raise ReviewOutputError("历史意见复核缺失、重复或编号不匹配")
    output._rechecks = rechecks
    return output


def _is_fixed_base_git_diff(command: str, comparison_sha: str, incremental: bool = False) -> bool:
    try:
        tokens = shlex.split(command)
        if tokens and Path(tokens[0]).name in {"bash", "dash", "sh", "zsh"}:
            if len(tokens) != 3 or tokens[1] not in {"-c", "-lc"}:
                return False
            tokens = shlex.split(tokens[2])
    except ValueError:
        return False
    if not tokens or Path(tokens.pop(0)).name != "git":
        return False
    if tokens and tokens[0] == "--no-pager":
        tokens.pop(0)
    if not tokens or tokens.pop(0) != "diff":
        return False
    if tokens and tokens[-1] == "--":
        tokens.pop()
    revisions = []
    for token in tokens:
        if token.startswith("-"):
            if token not in _SAFE_DIFF_OPTIONS and not _SAFE_DIFF_OPTION_VALUE.fullmatch(token):
                return False
        else:
            revisions.append(token)
    if incremental:
        return revisions == [f"{comparison_sha}...HEAD"]
    return revisions in [[f"{comparison_sha}...HEAD"], ["coderus-review-base...HEAD"]]


def validate_findings(output: ReviewOutput, ranges: ChangedRanges) -> ReviewOutput:
    validated_findings = []
    changed = False
    for finding in output.findings:
        clipped = ranges.clip(
            finding.file_path,
            finding.line_side,
            finding.line_start,
            finding.line_end,
        )
        if clipped is None:
            changed = True
            continue
        if clipped != (finding.line_start, finding.line_end):
            finding = finding.model_copy(
                update={"line_start": clipped[0], "line_end": clipped[1]}
            )
            changed = True
        validated_findings.append(finding)
    if changed:
        output = output.model_copy(update={"findings": validated_findings})
    output._comparison_sha = ranges.comparison_sha
    return output


def unlocated_findings(output: ReviewOutput, ranges: ChangedRanges) -> list[dict]:
    pending = []
    for finding in output.findings:
        reason = ranges.rejection_reason(
            finding.file_path, finding.line_side, finding.line_start, finding.line_end
        )
        if reason is not None:
            pending.append({"finding": finding.model_dump(mode="json"), "reason": reason})
    return pending


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
    additions: int | None = None,
    deletions: int | None = None,
    filtered_finding_count: int = 0,
    comparison_sha: str | None = None,
    pending_findings: list[dict] | None = None,
    standards_count: int | None = None,
    spec_reviewed: bool = False,
    previous: PreviousReview | None = None,
    checked: list[tuple[TrackedFinding, FindingRecheck]] | None = None,
    incremental: bool = False,
) -> tuple[str, str]:
    hosts = {"github": "github.com", "gitcode": "gitcode.com"}
    if provider not in hosts:
        raise ValueError("unsupported PR review provider")
    marker = f"<!-- coderus-pr-review:{review_key}:{base_sha}:{head_sha} -->"
    groups = (
        [("", _unique_findings(output.findings))]
        if standards_count is None
        else [
            ("Standards（代码规范）", _unique_findings(output.findings[:standards_count])),
            ("Spec（需求规格）", _unique_findings(output.findings[standards_count:])),
        ]
    )
    count = sum(len(findings) for _, findings in groups)
    if count:
        summary = f"发现 {count} 项需要处理的问题"
        if filtered_finding_count:
            summary += f"；另有 {filtered_finding_count} 条意见因无法安全定位未发布"
    elif filtered_finding_count:
        summary = f"{filtered_finding_count} 条意见因无法安全定位未发布，需人工复核"
    else:
        summary = "未发现需要反馈的具体问题"
    sections = [
        "## Coderus 代码检视",
        f"- 检视版本：{_code_span(head_sha)}",
    ]
    if previous:
        mode = "复检" if incremental else "全量检视（含历史复核）"
        previous_link = _previous_review_link(previous, hosts[provider])
        sections.append(f"- 本轮模式：{mode}，对照 {previous_link}")
    elif standards_count is not None:
        sections.append("- 本轮模式：全量检视")
    if changed_file_count is not None:
        input_summary = f"{changed_file_count} 个变更文件"
        if additions is not None and deletions is not None:
            input_summary += f"，+{additions} / -{deletions}"
        sections.append(f"- 检视输入：{input_summary}")
    sections.append("- PR 修改摘要：")
    sections.extend(
        f"  {index}. {_escape_markdown(sentence)}"
        for index, sentence in enumerate(output.change_summary, start=1)
    )
    sections.append(f"- 结论：{summary}")
    if pending_findings:
        sections[-1] = (
            f"- 结论：{count} 条已定位意见；"
            f"{len(pending_findings)} 条意见待人工确认（位置未验证）"
        )
    if previous:
        statuses = [check.status for _, check in checked or ()]
        sections[-1] = (
            f"- 本轮结果：新增 {count} 条，已解决 {statuses.count('resolved')} 条，"
            f"仍待处理 {statuses.count('open')} 条，"
            f"待人工确认 {statuses.count('needs_confirmation') + len(pending_findings or ())} 条。"
        )

    index = 0
    for label, findings in groups:
        if label:
            sections.extend(["", f"## {label}"])
            if label.startswith("Spec") and not spec_reviewed:
                sections.append("未找到可核实的需求来源，已跳过规格检视。")
                continue
            axis = "standards" if label.startswith("Standards") else "spec"
            pending_count = sum(
                item.get("review_axis") == axis for item in pending_findings or ()
            )
            sections.append(f"已定位 {len(findings)} 条，待人工确认 {pending_count} 条。")
            if findings:
                highest = min(findings, key=lambda finding: finding.priority)
                sections.append(f"本项优先处理：{_escape_markdown(highest.title)}")
        for finding in findings:
            index += 1
            sections.extend(_render_finding(
                finding, index, hosts[provider], owner, name,
                comparison_sha or output._comparison_sha or base_sha, head_sha,
            ))

    if checked:
        labels = {"resolved": "已解决", "open": "仍待处理", "needs_confirmation": "待人工确认"}
        sections.extend(["", "## 历史意见复核"])
        for old, check in checked:
            sections.append(
                f"- {labels[check.status]} · `{old.id}` {_escape_markdown(old.finding.title)}："
                f"{_escape_markdown(check.reason)}"
            )

    if pending_findings:
        sections.extend(["", "## 待人工确认（位置未验证）", "",
                         "以下为模型提出的疑似问题，尚未确认其位置及有效性，不作为已确认缺陷。"])
        for index, item in enumerate(pending_findings, 1):
            finding = item["finding"]
            side = "原版本" if finding["line_side"] == "LEFT" else "新版本"
            label = {"standards": "代码规范", "spec": "需求规格"}.get(item.get("review_axis"))
            source = f"（{label}）" if label else ""
            sections.extend([
                "", f"### 待确认 {index}{source}. {_escape_markdown(finding['title'])}", "",
                f"- 模型建议优先级：{_PRIORITY_LABELS[finding['priority']]}",
                f"- 模型所指文件：{_code_span(finding['file_path'])}",
                (f"- 模型所指范围（未验证）：{side}第 "
                 f"{finding['line_start']}-{finding['line_end']} 行"),
                f"- 无法定位原因：{_escape_markdown(item['reason'])}",
                f"- 问题说明：{_escape_markdown(finding['problem'])}",
                f"- 影响分析：{_escape_markdown(finding['impact'])}",
                f"- 修改建议：{_escape_markdown(finding['suggestion'])}",
            ])
    sections.extend(["", "> 本次为静态代码检视，未执行项目测试。", marker])
    return "\n".join(sections), marker


def _previous_review_link(previous: PreviousReview, host: str) -> str:
    label = f"RV-{previous.comment_task_id}"
    try:
        url = urlsplit(previous.comment_url)
    except ValueError:
        return label
    if url.scheme != "https" or url.netloc != host:
        return label
    return f"[{label}]({quote(previous.comment_url, safe=':/?#=%&@-_.~')})"


def _render_finding(
    finding: ReviewFinding,
    index: int,
    host: str,
    owner: str,
    name: str,
    comparison_sha: str,
    head_sha: str,
) -> list[str]:
    is_left = finding.line_side == "LEFT"
    sha = comparison_sha if is_left else head_sha
    version_label = "原版本" if is_left else "新版本"
    path = quote(finding.file_path, safe="/")
    url = (
        f"https://{host}/{quote(owner, safe='')}/{quote(name, safe='')}/blob/"
        f"{quote(sha, safe='')}/{path}#L{finding.line_start}-L{finding.line_end}"
    )
    return [
        "",
        f"### {index}. [{_PRIORITY_LABELS[finding.priority]}] {_escape_markdown(finding.title)}",
        "",
        f"- 文件：{_code_span(finding.file_path)}",
        f"- 代码范围：[{version_label}第 {finding.line_start}-{finding.line_end} 行]({url})",
        f"- 问题说明：{_escape_markdown(finding.problem)}",
        f"- 影响分析：{_escape_markdown(finding.impact)}",
        f"- 修改建议：{_escape_markdown(finding.suggestion)}",
    ]


def _unique_findings(source: list[ReviewFinding]) -> list[ReviewFinding]:
    seen: set[tuple[str, str, int, int, str, str, str]] = set()
    findings = []
    for finding in source:
        location = (
            finding.file_path,
            finding.line_side,
            finding.line_start,
            finding.line_end,
            finding.priority,
            finding.title,
            finding.problem,
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
