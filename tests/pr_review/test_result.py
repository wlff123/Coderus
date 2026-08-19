import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coderus.pr_review.models import ChangedRanges, ReviewFinding, ReviewOutput
from coderus.pr_review.result import (
    ReviewOutputError,
    parse_review_output,
    render_pr_comment,
    validate_findings,
)

BASE = "a" * 40
HEAD = "b" * 40
REVIEW_KEY = "random-review-key"
CHANGE_SUMMARY = ["新增技能沉淀流程。", "扩展服务接口。"]


def finding_data(**updates: object) -> dict[str, object]:
    finding = {
        "priority": "P1",
        "title": "空值未处理",
        "file_path": "src/app.py",
        "line_side": "RIGHT",
        "line_start": 12,
        "line_end": 14,
        "problem": "会抛异常",
        "impact": "请求失败",
        "suggestion": "处理 None",
    }
    finding.update(updates)
    return finding


def review_data(findings: list[dict[str, object]]) -> dict[str, object]:
    return {"change_summary": CHANGE_SUMMARY, "findings": findings}


def output_at_line(line: int, **updates: object) -> ReviewOutput:
    return ReviewOutput.model_validate(
        review_data(
            [finding_data(**{"line_start": line, "line_end": line, **updates})]
        )
    )


@pytest.fixture
def changed_ranges() -> ChangedRanges:
    return ChangedRanges(
        {
            ("src/app.py", "RIGHT"): ((10, 20),),
            ("src/removed.py", "LEFT"): ((3, 5),),
        }
    )


@pytest.fixture
def valid_output() -> ReviewOutput:
    return output_at_line(12, line_end=14)


def test_parse_review_output_accepts_json_block() -> None:
    output = parse_review_output(
        "review complete\n```json\n"
        + json.dumps(review_data([finding_data()]), ensure_ascii=False)
        + "\n```"
    )

    assert output.findings[0].line_end == 14


@pytest.mark.parametrize(
    "suffix",
    [
        "review complete",
        "```json\n{}\n```",
    ],
)
def test_parse_review_output_rejects_non_whitespace_after_json_block(suffix: str) -> None:
    payload = "review complete\n```json\n" + json.dumps({"findings": []}) + f"\n```\n{suffix}"

    with pytest.raises(ReviewOutputError):
        parse_review_output(payload)


def test_parse_review_output_accepts_complete_json() -> None:
    output = parse_review_output(json.dumps(review_data([]), ensure_ascii=False))

    assert output.change_summary == CHANGE_SUMMARY
    assert output.findings == []


def test_parse_review_output_accepts_codex_jsonl_final_message() -> None:
    message = json.dumps(review_data([finding_data()]), ensure_ascii=False)
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": message},
                },
                ensure_ascii=False,
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
        ]
    )

    output = parse_review_output(stdout)

    assert output.findings[0].title == "空值未处理"


def native_review_stdout(
    message: str,
    *,
    command_exit_code: int = 0,
    command: str | None = None,
) -> str:
    command_status = "completed" if command_exit_code == 0 else "failed"
    command = command or f"git diff {BASE}...HEAD"
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": command,
                        "aggregated_output": "",
                        "exit_code": command_exit_code,
                        "status": command_status,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": message},
                },
                ensure_ascii=False,
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )


def test_parse_review_output_accepts_native_review_message() -> None:
    message = """本次变更增加了配置校验并调整了请求处理流程。

Review comment:

- [P1] 空值会导致请求失败 — RIGHT /srv/work/pr-review-1/src/app.py:12-14
  问题：当输入为空时会直接解引用。
  影响：请求会抛出异常并中断。
  建议：在读取属性前处理空值。"""
    ranges = ChangedRanges({("src/app.py", "RIGHT"): ((12, 14),)})

    output = parse_review_output(
        native_review_stdout(message),
        workspace=Path("/srv/work/pr-review-1"),
        ranges=ranges,
        fallback_summary="本次 PR 涉及 1 个变更文件，新增 3 行并删除 0 行。",
        comparison_sha=BASE,
    )

    assert output.change_summary == ["本次变更增加了配置校验并调整了请求处理流程。"]
    finding = output.findings[0]
    assert finding.priority == "P1"
    assert finding.title == "空值会导致请求失败"
    assert finding.file_path == "src/app.py"
    assert finding.line_side == "RIGHT"
    assert (finding.line_start, finding.line_end) == (12, 14)
    assert "直接解引用" in finding.problem
    assert "请求会抛出异常" in finding.impact
    assert finding.suggestion == "在读取属性前处理空值。"


def test_parse_review_output_uses_stats_summary_when_native_review_has_no_findings() -> None:
    output = parse_review_output(
        native_review_stdout(
            "本次变更调整了配置校验和错误处理。\n\n未发现需要反馈的具体问题。"
        ),
        workspace=Path("/srv/work/pr-review-1"),
        ranges=ChangedRanges({}),
        fallback_summary="本次 PR 涉及 2 个变更文件，新增 8 行并删除 1 行。",
        comparison_sha=BASE,
    )

    assert output.change_summary == ["本次变更调整了配置校验和错误处理。"]
    assert output.findings == []


def test_parse_review_output_accepts_native_no_findings_wording() -> None:
    message = (
        "本次修改迁移了会话释放流程，并增加了身份保存和释放重试。"
        "静态检视未发现本次变更引入的明确、可执行问题。"
    )

    output = parse_review_output(
        native_review_stdout(message),
        workspace=Path("/srv/work/pr-review-1"),
        ranges=ChangedRanges({}),
        fallback_summary="本次 PR 涉及 2 个变更文件，新增 8 行并删除 1 行。",
        comparison_sha=BASE,
    )

    assert output.change_summary == [
        "本次修改迁移了会话释放流程，并增加了身份保存和释放重试。"
    ]
    assert output.findings == []


def test_parse_review_output_accepts_no_findings_review_comment_prefix() -> None:
    output = parse_review_output(
        native_review_stdout(
            "Review comment:\n\n"
            "本次变更完成了检视流程调整。\n"
            "未发现需要修改的问题。"
        ),
        workspace=Path("/srv/work/pr-review-1"),
        ranges=ChangedRanges({}, comparison_sha=BASE),
        fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
        comparison_sha=BASE,
    )

    assert output.change_summary == ["本次变更完成了检视流程调整。"]
    assert output.findings == []


def test_parse_review_output_rejects_native_result_when_git_inspection_failed() -> None:
    with pytest.raises(ReviewOutputError, match="Git"):
        parse_review_output(
            native_review_stdout(
                "未发现可明确归因于本次变更且需要修复的问题。",
                command_exit_code=1,
            ),
            workspace=Path("/srv/work/pr-review-1"),
            ranges=ChangedRanges({}),
            fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
            comparison_sha=BASE,
        )


def test_native_json_output_still_requires_successful_git_inspection() -> None:
    message = json.dumps(review_data([]), ensure_ascii=False)

    with pytest.raises(ReviewOutputError, match="Git"):
        parse_review_output(
            native_review_stdout(message, command_exit_code=1),
            workspace=Path("/srv/work/pr-review-1"),
            ranges=ChangedRanges({}),
            fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
            comparison_sha=BASE,
        )


def test_native_context_rejects_raw_json_without_agent_event() -> None:
    with pytest.raises(ReviewOutputError, match="Agent"):
        parse_review_output(
            json.dumps(review_data([]), ensure_ascii=False),
            workspace=Path("/srv/work/pr-review-1"),
            ranges=ChangedRanges({}),
            fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
            comparison_sha=BASE,
        )


@pytest.mark.parametrize(
    "message",
    [
        "Review comment:\n- [P1] 损坏的格式: src/app.py:12\n  问题说明。",
        "Review comment:\n- [P1] 缺少结构化正文 — RIGHT src/app.py:12\n  普通正文。",
        "Review comment:\n- [P4] 未知优先级 — RIGHT src/app.py:12\n"
        "  普通正文。\n\n未发现需要反馈的具体问题。",
    ],
)
def test_native_review_rejects_unrecognized_finding_format(message: str) -> None:
    with pytest.raises(ReviewOutputError, match="格式"):
        parse_review_output(
            native_review_stdout(message),
            workspace=Path("/srv/work/pr-review-1"),
            ranges=ChangedRanges({("src/app.py", "RIGHT"): ((12, 12),)}),
            fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
            comparison_sha=BASE,
        )


@pytest.mark.parametrize(
    "command",
    [
        "printf 'git diff aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
        "git diff unrelated-branch...HEAD",
        f"git diff {BASE}...HEAD || true",
        f"git diff unrelated-branch...HEAD -- fixtures/{BASE}.txt",
        f"echo -c 'git diff {BASE}...HEAD'",
        f"git -C /tmp diff {BASE}...HEAD",
        f"git diff --no-index {BASE}...HEAD",
        f"git diff {BASE} {BASE}",
        f"git diff {BASE}...HEAD &",
        f"git diff --src-prefix {BASE}...HEAD",
        f"git diff -S {BASE}",
    ],
)
def test_native_review_rejects_untrusted_git_evidence(command: str) -> None:
    with pytest.raises(ReviewOutputError, match="Git"):
        parse_review_output(
            native_review_stdout("未发现需要反馈的具体问题。", command=command),
            workspace=Path("/srv/work/pr-review-1"),
            ranges=ChangedRanges({}),
            fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
            comparison_sha=BASE,
        )


def test_native_review_accepts_safe_git_global_options_for_the_fixed_base() -> None:
    output = parse_review_output(
        native_review_stdout(
            "未发现需要反馈的具体问题。",
            command=f"git --no-pager diff {BASE}...HEAD",
        ),
        workspace=Path("/srv/work/pr-review-1"),
        ranges=ChangedRanges({}),
        fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
        comparison_sha=BASE,
    )

    assert output.findings == []


@pytest.mark.parametrize(
    "message",
    [
        "本次变更调整了配置。\n- [PX] 疑似问题\n未发现需要反馈的具体问题。",
        "本次变更调整了配置。\n- P1 疑似问题\n未发现需要反馈的具体问题。",
        "本次变更调整了配置。\n+ P1 疑似问题\n未发现需要反馈的具体问题。",
        "本次变更调整了配置。\n未发现需要反馈的具体问题。\n额外结论。",
    ],
)
def test_native_review_rejects_finding_like_text_in_no_findings_result(message: str) -> None:
    with pytest.raises(ReviewOutputError, match="格式"):
        parse_review_output(
            native_review_stdout(message),
            workspace=Path("/srv/work/pr-review-1"),
            ranges=ChangedRanges({}),
            fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
            comparison_sha=BASE,
        )


def test_native_review_rejects_overlong_finding_instead_of_truncating() -> None:
    message = f"""修改摘要：本次变更调整了空值处理。

Review comment:

- [P1] {"长" * 201} — RIGHT src/app.py:12
  问题：输入为空时会直接解引用。
  影响：请求会抛出异常。
  建议：在读取属性前处理空值。"""

    with pytest.raises(ReviewOutputError, match="格式"):
        parse_review_output(
            native_review_stdout(message),
            workspace=Path("/srv/work/pr-review-1"),
            ranges=ChangedRanges({("src/app.py", "RIGHT"): ((12, 12),)}),
            fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 0 行。",
            comparison_sha=BASE,
        )


def test_native_review_uses_explicit_line_side_when_line_numbers_overlap() -> None:
    message = """本次变更替换了鉴权检查。

Review comment:

- [P1] 删除了必要的鉴权检查 — LEFT src/app.py:12
  问题：旧版本中的鉴权调用被删除。
  影响：请求可能绕过访问控制。
  建议：保留鉴权调用或增加等价校验。"""
    ranges = ChangedRanges(
        {
            ("src/app.py", "LEFT"): ((12, 12),),
            ("src/app.py", "RIGHT"): ((12, 12),),
        }
    )

    output = parse_review_output(
        native_review_stdout(message),
        workspace=Path("/srv/work/pr-review-1"),
        ranges=ranges,
        fallback_summary="本次 PR 涉及 1 个变更文件，新增 1 行并删除 1 行。",
        comparison_sha=BASE,
    )

    assert output.findings[0].line_side == "LEFT"


@pytest.mark.parametrize(
    "payload",
    [
        "review complete",
        "```json\n{}\n```\n```json\n{}\n```",
        "```JSON\n{}\n```",
    ],
)
def test_parse_review_output_rejects_anything_but_complete_json_or_one_json_block(
    payload: str,
) -> None:
    with pytest.raises(ReviewOutputError):
        parse_review_output(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", "field"),
        ("priority", "P4"),
        ("title", ""),
        ("problem", " "),
        ("impact", ""),
        ("suggestion", ""),
        ("line_start", True),
        ("line_end", False),
        ("line_end", 11),
    ],
)
def test_parse_review_output_rejects_invalid_finding(field: str, value: object) -> None:
    with pytest.raises(ReviewOutputError):
        parse_review_output(
            json.dumps(review_data([finding_data(**{field: value})]), ensure_ascii=False)
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"findings": []},
        {"change_summary": [], "findings": []},
        {"change_summary": ["Only English."], "findings": []},
        {"change_summary": [f"第 {index} 项。" for index in range(6)], "findings": []},
    ],
)
def test_parse_review_output_requires_one_to_five_chinese_summary_sentences(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ReviewOutputError):
        parse_review_output(json.dumps(payload, ensure_ascii=False))


def test_review_finding_forbids_extra_fields_and_requires_chinese_text() -> None:
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(finding_data(unexpected="field"))
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(finding_data(title="missing Chinese"))


def test_parse_review_output_rejects_overlong_stdout() -> None:
    with pytest.raises(ReviewOutputError, match="过长"):
        parse_review_output(" " * 65_537)


def test_validate_drops_location_outside_diff(changed_ranges: ChangedRanges) -> None:
    validated = validate_findings(output_at_line(99), changed_ranges)

    assert validated.findings == []


def test_validate_clips_location_to_single_overlapping_diff(
    changed_ranges: ChangedRanges,
) -> None:
    output = output_at_line(9, line_end=12)

    validated = validate_findings(output, changed_ranges)

    assert validated.findings[0].line_start == 10
    assert validated.findings[0].line_end == 12


def test_validate_accepts_location_contained_in_one_hunk(
    changed_ranges: ChangedRanges, valid_output: ReviewOutput
) -> None:
    assert validate_findings(valid_output, changed_ranges) is valid_output


def test_validate_drops_location_crossing_changed_hunks() -> None:
    ranges = ChangedRanges(
        {("src/app.py", "RIGHT"): ((10, 12), (20, 22))}
    )
    output = output_at_line(12, line_end=20)

    assert validate_findings(output, ranges).findings == []


def test_validate_drops_oversized_location_covering_changed_lines(
    changed_ranges: ChangedRanges,
) -> None:
    output = output_at_line(1, line_end=100_000)

    assert validate_findings(output, changed_ranges).findings == []


def test_validate_keeps_valid_findings_when_dropping_invalid_ones(
    changed_ranges: ChangedRanges,
) -> None:
    output = ReviewOutput.model_validate(
        review_data(
            [
                finding_data(line_start=12, line_end=14),
                finding_data(line_start=99, line_end=99, title="越界意见"),
            ]
        )
    )

    validated = validate_findings(output, changed_ranges)

    assert [finding.line_start for finding in validated.findings] == [12]


def test_render_comment_contains_clickable_range(valid_output: ReviewOutput) -> None:
    body, marker = render_pr_comment(
        "github",
        valid_output,
        "acme",
        "widgets",
        BASE,
        HEAD,
        review_key=REVIEW_KEY,
    )

    assert "`src/app.py`" in body
    assert f"/blob/{HEAD}/src/app.py#L12-L14" in body
    assert "问题说明" in body and "影响分析" in body and "修改建议" in body
    assert marker == f"<!-- coderus-pr-review:{REVIEW_KEY}:{BASE}:{HEAD} -->"
    assert body.endswith(marker)


@pytest.mark.parametrize(
    ("provider", "host"),
    [("github", "github.com"), ("gitcode", "gitcode.com")],
)
def test_render_comment_uses_provider_file_links(
    valid_output: ReviewOutput, provider: str, host: str
) -> None:
    body, marker = render_pr_comment(
        provider,
        valid_output,
        "acme",
        "widgets",
        BASE,
        HEAD,
        REVIEW_KEY,
    )

    assert f"https://{host}/acme/widgets/blob/{HEAD}/src/app.py#L12-L14" in body
    assert marker == f"<!-- coderus-pr-review:{REVIEW_KEY}:{BASE}:{HEAD} -->"


def test_render_comment_uses_comparison_sha_for_left_locations() -> None:
    output = output_at_line(3, file_path="src/removed.py", line_side="LEFT", line_end=5)
    comparison = "c" * 40

    body, _ = render_pr_comment(
        "github",
        output,
        "acme",
        "widgets",
        BASE,
        HEAD,
        REVIEW_KEY,
        comparison_sha=comparison,
    )

    assert f"/blob/{comparison}/src/removed.py#L3-L5" in body
    assert f"/blob/{BASE}/src/removed.py#L3-L5" not in body
    assert f"/blob/{HEAD}/src/removed.py#L3-L5" not in body


def test_validated_findings_carry_comparison_sha_to_left_links() -> None:
    comparison = "c" * 40
    output = output_at_line(
        3, file_path="src/removed.py", line_side="LEFT", line_end=5
    )
    ranges = ChangedRanges(
        {("src/removed.py", "LEFT"): ((3, 5),)}, comparison_sha=comparison
    )

    validated = validate_findings(output, ranges)
    body, _ = render_pr_comment(
        "github", validated, "acme", "widgets", BASE, HEAD, REVIEW_KEY
    )

    assert f"/blob/{comparison}/src/removed.py#L3-L5" in body
    assert "comparison_sha" not in validated.model_dump()


def test_render_comment_allows_no_findings() -> None:
    body, marker = render_pr_comment(
        "github",
        ReviewOutput.model_validate(review_data([])),
        "acme",
        "widgets",
        BASE,
        HEAD,
        REVIEW_KEY,
        changed_file_count=39,
    )

    assert "检视输入：39 个变更文件" in body
    assert "PR 修改摘要" in body
    assert "1. 新增技能沉淀流程。" in body
    assert "2. 扩展服务接口。" in body
    assert "未发现需要反馈的具体问题" in body
    assert body.endswith(marker)


def test_render_comment_reports_filtered_findings_instead_of_clean_result() -> None:
    body, _ = render_pr_comment(
        "gitcode",
        ReviewOutput.model_validate(review_data([])),
        "acme",
        "widgets",
        BASE,
        HEAD,
        REVIEW_KEY,
        changed_file_count=13,
        additions=779,
        deletions=29,
        filtered_finding_count=2,
    )

    assert "检视输入：13 个变更文件，+779 / -29" in body
    assert "2 条意见因无法安全定位未发布" in body
    assert "未发现需要反馈的具体问题" not in body


def test_render_comment_escapes_markdown_and_quotes_file_paths() -> None:
    output = output_at_line(
        12,
        file_path="src/a b#c.py",
        title="标题\n## 注入",
        problem="问题 **粗体** <script>",
    )

    body, _ = render_pr_comment("github", output, "acme", "widgets", BASE, HEAD, REVIEW_KEY)

    assert "/src/a%20b%23c.py#L12-L12" in body
    assert "\n## 注入" not in body
    assert "\\#" in body
    assert "&lt;script&gt;" in body


def test_render_comment_keeps_malicious_path_inside_a_safe_code_span() -> None:
    malicious_path = (
        f"src/````<b><!-- coderus-pr-review:{REVIEW_KEY}:{BASE}:{HEAD} --></b>.py"
    )
    output = output_at_line(12, file_path=malicious_path)

    body, marker = render_pr_comment(
        "github", output, "acme", "widgets", BASE, HEAD, REVIEW_KEY
    )

    assert (
        f"`````src/````&lt;b&gt;&lt;!-- coderus-pr-review:{REVIEW_KEY}:{BASE}:{HEAD} --&gt;"
        in body
    )
    assert body.count("<!--") == 1
    assert body.count(marker) == 1
    assert body.count("### ") == 1
    assert body.endswith(marker)


def test_render_comment_keeps_distinct_findings_at_the_same_location() -> None:
    first = finding_data(title="第一个问题")
    duplicate = finding_data(title="第二个问题", impact="不同影响")
    output = ReviewOutput.model_validate(review_data([first, duplicate]))

    body, _ = render_pr_comment("github", output, "acme", "widgets", BASE, HEAD, REVIEW_KEY)

    assert body.count("### ") == 2
    assert "第一个问题" in body
    assert "第二个问题" in body


def test_render_comment_deduplicates_identical_findings() -> None:
    finding = finding_data()
    output = ReviewOutput.model_validate(review_data([finding, finding.copy()]))

    body, _ = render_pr_comment("github", output, "acme", "widgets", BASE, HEAD, REVIEW_KEY)

    assert body.count("### ") == 1
