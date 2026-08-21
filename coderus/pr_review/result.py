from __future__ import annotations

import html
import json
import re
import shlex
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError

from coderus.providers import ProviderName

from .models import ChangedRanges, ReviewFinding, ReviewOutput, normalize_repository_path

MAX_REVIEW_OUTPUT_CHARS = 65_536
_FENCED_JSON_BLOCK = re.compile(r"```json[ \t]*\r?\n(?P<payload>.*?)\r?\n```", re.DOTALL)
_NATIVE_FINDING_HEADER = re.compile(
    r"(?m)^\s*(?:(?:#{1,6}\s*)?\d+[.)、]\s*)?(?:[-*+]\s*)?"
    r"(?:\*\*)?\[(P[0-3])(?:[^\]\r\n]*)\](?:\*\*)?\s+(.+?)\s+"
    r"(?:—|–|-)\s+(LEFT|RIGHT)\s+(.+?):(\d+)(?:-(\d+))?\s*\*{0,2}\s*$"
)
_NO_FINDINGS_CONCLUSION = re.compile(
    r"(?:静态检视)?(?:未发现|没有发现)(?:需要反馈的具体问题|"
    r"本次变更引入的明确[、，]?可执行问题|"
    r"可明确归因于本次变更且需要修复的问题|"
    r"需要(?:修改|修复)的问题|"
    r"可执行问题|明显问题|问题)"
)
_SAFE_DIFF_OPTIONS = {
    "--check",
    "--find-copies",
    "--find-renames",
    "--histogram",
    "--minimal",
    "--name-only",
    "--name-status",
    "--no-color",
    "--no-ext-diff",
    "--patience",
    "--stat",
}
_SAFE_DIFF_OPTION_VALUE = re.compile(
    r"(?:-U\d+|--unified=\d+|--find-(?:copies|renames)=\d+%?|--diff-filter=[A-Z*]+)"
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


def parse_review_output(
    stdout: str,
    *,
    workspace: Path | None = None,
    ranges: ChangedRanges | None = None,
    fallback_summary: str | None = None,
    comparison_sha: str | None = None,
) -> ReviewOutput:
    message = _final_agent_message(stdout)
    raw_payload = message if message is not None else stdout
    if len(raw_payload) > MAX_REVIEW_OUTPUT_CHARS:
        raise ReviewOutputError("检视输出过长")
    payload = raw_payload.strip()
    native_context = all(
        value is not None for value in (workspace, ranges, fallback_summary, comparison_sha)
    )
    if native_context:
        if message is None:
            raise ReviewOutputError("Codex 未返回有效的 Agent 消息事件")
        if not _has_successful_git_inspection(stdout, comparison_sha or ""):
            raise ReviewOutputError("Codex 未成功完成 Git 变更检查")

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        try:
            decoded = _extract_fenced_json(payload)
        except ReviewOutputError:
            if not native_context or message is None:
                raise
            assert workspace is not None
            assert ranges is not None
            return _parse_native_review_output(
                message,
                workspace=workspace,
                ranges=ranges,
                fallback_summary=fallback_summary or "",
            )

    try:
        return ReviewOutput.model_validate(decoded)
    except ValidationError as error:
        raise ReviewOutputError("检视结果格式无效") from error


def _parse_native_review_output(
    message: str,
    *,
    workspace: Path,
    ranges: ChangedRanges,
    fallback_summary: str,
) -> ReviewOutput:
    matches = list(_NATIVE_FINDING_HEADER.finditer(message))
    priority_markers = re.findall(r"\[P[^\]\r\n]*\]", message)
    if len(priority_markers) != len(matches):
        raise ReviewOutputError(
            "原生检视意见格式无效：优先级标记数量与意见标题数量不一致"
        )
    if not matches:
        _validate_no_findings_message(message)
    summaries = _native_change_summary(message, matches, fallback_summary)
    findings = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(message)
        body = "\n".join(
            line.strip()
            for line in message[match.end() : body_end].splitlines()
            if line.strip()
        )
        if not body:
            raise ReviewOutputError("原生检视意见缺少问题说明")
        side = match.group(3)
        path = _repository_relative_location(match.group(4), workspace)
        start = int(match.group(5))
        end = int(match.group(6) or match.group(5))
        problem, impact, suggestion = _structured_finding_details(body)
        try:
            findings.append(
                ReviewFinding(
                    priority=match.group(1),
                    title=match.group(2).strip(),
                    file_path=path,
                    line_side=side,
                    line_start=start,
                    line_end=end,
                    problem=problem,
                    impact=impact,
                    suggestion=suggestion,
                )
            )
        except ValidationError as error:
            raise ReviewOutputError("原生检视结果格式无效") from error
    try:
        return ReviewOutput(change_summary=summaries, findings=findings)
    except ValidationError as error:
        raise ReviewOutputError("原生检视结果格式无效") from error


def _has_successful_git_inspection(stdout: str, comparison_sha: str) -> bool:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "command_execution"
            and item.get("status") == "completed"
            and item.get("exit_code") == 0
            and isinstance(item.get("command"), str)
            and _is_fixed_base_git_diff(item["command"], comparison_sha)
        ):
            return True
    return False


def _is_fixed_base_git_diff(command: str, comparison_sha: str) -> bool:
    try:
        outer = shlex.split(command)
    except ValueError:
        return False
    if not outer:
        return False
    executable = Path(outer[0]).name
    if executable in {"bash", "dash", "sh", "zsh"}:
        if len(outer) != 3 or outer[1] not in {"-c", "-lc"}:
            return False
        script = outer[2]
    elif executable == "git":
        script = command
    else:
        return False
    if (
        "|" in script
        or ";" in script
        or "\n" in script
        or re.search(r"(?<!&)&(?!&)", script)
    ):
        return False
    try:
        segments = [shlex.split(segment.strip()) for segment in script.split("&&")]
    except ValueError:
        return False
    if not segments or not all(_is_safe_git_segment(tokens) for tokens in segments):
        return False
    return any(_is_target_git_diff(tokens, comparison_sha) for tokens in segments)


def _is_safe_git_segment(tokens: list[str]) -> bool:
    if not tokens or Path(tokens[0]).name != "git":
        return False
    index = 1
    while index < len(tokens) and tokens[index] == "--no-pager":
        index += 1
    return index < len(tokens) and tokens[index] in {"diff", "status"}


def _is_target_git_diff(tokens: list[str], comparison_sha: str) -> bool:
    index = 1
    while index < len(tokens) and tokens[index] == "--no-pager":
        index += 1
    if index >= len(tokens) or tokens[index] != "diff":
        return False
    arguments = tokens[index + 1 :]
    if "--no-index" in arguments:
        return False
    revision_arguments = arguments
    if "--" in revision_arguments:
        revision_arguments = revision_arguments[: revision_arguments.index("--")]
    if any(
        token.startswith("-")
        and token not in _SAFE_DIFF_OPTIONS
        and _SAFE_DIFF_OPTION_VALUE.fullmatch(token) is None
        for token in revision_arguments
    ):
        return False
    revision_arguments = [token for token in revision_arguments if not token.startswith("-")]
    return len(revision_arguments) == 1 and any(
        _references_comparison(revision_arguments[0], comparison)
        for comparison in (comparison_sha, "coderus-review-base")
    )


def _references_comparison(token: str, comparison: str) -> bool:
    return token in {
        comparison,
        f"{comparison}..HEAD",
        f"{comparison}...HEAD",
    }


def _native_change_summary(
    message: str, matches: list[re.Match[str]], fallback_summary: str
) -> list[str]:
    prefix = message[: matches[0].start()] if matches else message
    prefix = prefix.replace("Review comment:", "").strip()
    conclusion = _NO_FINDINGS_CONCLUSION.search(prefix)
    if conclusion is not None:
        prefix = prefix[: conclusion.start()].strip()
    prefix = prefix.replace("修改摘要：", "").strip()
    summaries = []
    for block in re.split(r"\n\s*\n|\n", prefix):
        summary = re.sub(r"^\s*\d+[.、]\s*", "", block).strip()
        summary = " ".join(summary.split())
        if summary and any("\u4e00" <= character <= "\u9fff" for character in summary):
            summaries.append(summary)
    summaries = [summary for summary in summaries if summary][:5]
    return summaries or [fallback_summary]


def _validate_no_findings_message(message: str) -> None:
    normalized = message.strip()
    if normalized.startswith("Review comment:"):
        normalized = normalized[len("Review comment:") :].lstrip()
    match = re.fullmatch(
        rf"(?s)(?P<summary>.*?)\n*(?:{_NO_FINDINGS_CONCLUSION.pattern})。?\s*",
        normalized,
    )
    if match is None:
        raise ReviewOutputError("原生检视结论格式无效")
    summary = match.group("summary")
    if "Review comment:" in summary or re.search(
        r"(?m)^\s*(?:[+*-]\s+|(?:问题|影响|建议)[：:])", summary
    ):
        raise ReviewOutputError("原生检视结论格式无效")


def _repository_relative_location(value: str, workspace: Path) -> str:
    location = value.strip().strip("`*_[]() ").replace("\\", "/")
    workspace_prefix = str(workspace).replace("\\", "/").rstrip("/") + "/"
    if location.casefold().startswith(workspace_prefix.casefold()):
        location = location[len(workspace_prefix) :]
    normalized = normalize_repository_path(location)
    if normalized is None:
        raise ReviewOutputError("原生检视意见包含无效文件路径")
    return normalized


def _structured_finding_details(body: str) -> tuple[str, str, str]:
    normalized_lines: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(
            r"^(?:[-*+]\s+)?(?:\*\*)?(问题|影响|建议)(?:\*\*)?\s*[:：]\s*",
            r"\1：",
            line,
        )
        normalized_lines.append(line)
    fields: dict[str, str] = {}
    current: str | None = None
    for line in normalized_lines:
        match = re.fullmatch(r"(?P<label>问题|影响|建议)：?\s*(?P<value>.*)", line)
        if match is not None:
            label = match.group("label")
            if label in fields:
                raise ReviewOutputError("原生检视意见包含重复正文标签")
            current = label
            value = match.group("value").strip()
            if value:
                fields[label] = value
            continue
        if current is None or current in fields:
            raise ReviewOutputError("原生检视意见格式无效")
        fields[current] = line
    if set(fields) != {"问题", "影响", "建议"}:
        raise ReviewOutputError("原生检视意见格式无效")
    return (
        fields["问题"],
        fields["影响"],
        fields["建议"],
    )


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
) -> tuple[str, str]:
    hosts = {"github": "github.com", "gitcode": "gitcode.com"}
    if provider not in hosts:
        raise ValueError("unsupported PR review provider")
    marker = f"<!-- coderus-pr-review:{review_key}:{base_sha}:{head_sha} -->"
    findings = _unique_findings(output)
    if findings:
        summary = f"发现 {len(findings)} 项需要处理的问题"
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

    for index, finding in enumerate(findings, start=1):
        sha = (
            (comparison_sha or output._comparison_sha or base_sha)
            if finding.line_side == "LEFT"
            else head_sha
        )
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
    seen: set[tuple[str, str, int, int, str, str, str]] = set()
    findings = []
    for finding in output.findings:
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
