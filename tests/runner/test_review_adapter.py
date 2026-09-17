from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coderus.runner import (
    AgentRole,
    JobResult,
    JobSpec,
    JobStatus,
    LocalCodexRunner,
    RunnerConfig,
    Stage,
)
from coderus.runner.review_adapter import (
    NO_FINDINGS_MARKER,
    ReviewAdaptationError,
    final_message,
    finding_inventory,
    formatter_prompt,
    validate_conversion,
)

NATIVE = (
    "本次修改了除法函数。\n\nReview comment:\n\n"
    "- [P1] 恢复除数参数 — /workspace/divide.py:2-2\n"
    "  RIGHT。问题：除数固定为零。影响：调用失败。建议：恢复参数。"
)
FINDING = {
    "priority": "P1",
    "title": "恢复除数参数",
    "file_path": "divide.py",
    "line_side": "RIGHT",
    "line_start": 2,
    "line_end": 2,
    "problem": "除数固定为零。",
    "impact": "调用失败。",
    "suggestion": "恢复参数。",
}


def events(message: str) -> str:
    return "\n".join(
        json.dumps(event)
        for event in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
            {"type": "turn.completed"},
        ]
    )


def output(findings: list[dict]) -> str:
    return json.dumps({"change_summary": ["本次修改了除法函数。"], "findings": findings})


def test_native_inventory_and_conversion_preserve_all_findings() -> None:
    native = NATIVE + "\n- [P2] 修复第二个问题 — /workspace/other.py:8-9\n说明。"
    assert finding_inventory(native) == [("P1", "恢复除数参数"), ("P2", "修复第二个问题")]
    second = dict(
        FINDING,
        priority="P2",
        title="修复第二个问题",
        file_path="other.py",
        line_start=8,
        line_end=9,
    )
    validate_conversion(native, output([FINDING, second]), workspace="/workspace")
    assert "不可信" in formatter_prompt(native, "/workspace")


@pytest.mark.parametrize(
    "message",
    [
        "",
        "未完成检视。",
        "No issues found.",
        "- [P1] 标题没有位置",
        NATIVE + "\n" + NO_FINDINGS_MARKER,
        NO_FINDINGS_MARKER + "\n但检视尚未完成。",
    ],
)
def test_ambiguous_or_incomplete_native_result_cannot_become_lgtm(message: str) -> None:
    with pytest.raises(ReviewAdaptationError):
        finding_inventory(message)


def test_explicit_completed_empty_review_can_be_converted() -> None:
    native = "本次仅调整了括号。\n" + NO_FINDINGS_MARKER
    assert finding_inventory(native) == []
    validate_conversion(native, output([]), workspace="/workspace")


@pytest.mark.parametrize(
    "findings",
    [
        [],
        [FINDING, FINDING],
        [dict(FINDING, priority="P2")],
        [dict(FINDING, title="另一个问题")],
        [dict(FINDING, file_path="other.py")],
        [dict(FINDING, line_start=3)],
        [dict(FINDING, line_side="LEFT")],
    ],
)
def test_conversion_cannot_drop_add_or_move_findings(findings: list[dict]) -> None:
    with pytest.raises(ReviewAdaptationError):
        validate_conversion(NATIVE, output(findings), workspace="/workspace")


def test_explicit_left_location_is_preserved() -> None:
    validate_conversion(
        NATIVE.replace("RIGHT。", "LEFT。"),
        output([dict(FINDING, line_side="LEFT")]),
        workspace="/workspace",
    )


@pytest.mark.parametrize(
    "stdout",
    [
        NATIVE,
        events(NATIVE).splitlines()[0],
        events(NATIVE) + '\n{"type":"turn.failed"}',
        events(NATIVE) + '\n{"type":"error"}',
        events("x" * 65_537),
    ],
    ids=["raw-text", "incomplete", "turn-failed", "error", "oversized"],
)
def test_final_message_requires_successful_complete_event_stream(stdout: str) -> None:
    with pytest.raises(ReviewAdaptationError):
        final_message(stdout)


def test_formatter_tool_calls_are_rejected() -> None:
    stdout = (
        events(output([FINDING]))
        + "\n"
        + json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution"},
            }
        )
    )
    with pytest.raises(ReviewAdaptationError, match="工具"):
        final_message(stdout, allow_tools=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "drop",
        "native_failure",
        "convert_failure",
        "cancel",
        "truncated",
        "tools",
        "timeout",
    ],
)
async def test_two_stage_review_safety_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    spec = JobSpec(
        "review-1",
        Stage.PR_REVIEW,
        AgentRole.PR_REVIEWER,
        workspace,
        "检视规范",
        output_schema=schema,
        review_base="coderus-review-base",
        timeout_seconds=60,
        max_output_bytes=100_000,
    )
    runner = LocalCodexRunner(RunnerConfig(tmp_path, ("codex",)))
    calls = []
    cancelled = asyncio.Event()
    evidence = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "git diff coderus-review-base...HEAD",
                "status": "completed",
                "exit_code": 0,
            },
        }
    )
    native = NATIVE.replace("/workspace", str(workspace).replace("\\", "/"))

    async def process(job, command, *args, stdin_text=None, **kwargs):
        calls.append((job, command, stdin_text))
        if len(calls) == 1:
            if scenario == "cancel":
                cancelled.set()
            if scenario == "timeout":
                monkeypatch.setattr(
                    "coderus.runner.local.time", SimpleNamespace(monotonic=lambda: 10**12)
                )
            return JobResult(
                job.job_id,
                JobStatus.FAILED if scenario == "native_failure" else JobStatus.SUCCEEDED,
                1 if scenario == "native_failure" else 0,
                evidence + "\n" + events(native),
                "",
                scenario == "truncated",
                1,
            )
        stdout = events(output([] if scenario == "drop" else [FINDING]))
        if scenario == "tools":
            stdout += "\n" + evidence
        return JobResult(
            job.job_id,
            JobStatus.FAILED if scenario == "convert_failure" else JobStatus.SUCCEEDED,
            1 if scenario == "convert_failure" else 0,
            stdout,
            "",
            False,
            1,
        )

    monkeypatch.setattr(runner, "_run_process", process)
    result = await runner.run(spec, cancel_event=cancelled)
    assert "review" in calls[0][1]
    assert "--output-schema" not in calls[0][1]
    assert calls[0][2] is None
    if scenario in {"native_failure", "cancel", "truncated", "timeout"}:
        assert len(calls) == 1
    else:
        job, command, stdin_text = calls[1]
        assert "review" not in command
        assert "--output-schema" in command
        assert command[-1] == "-"
        assert "native_review" in stdin_text
        assert 0 < job.timeout_seconds <= spec.timeout_seconds
        assert 0 < job.max_output_bytes < spec.max_output_bytes
    if scenario == "success":
        assert result.status is JobStatus.SUCCEEDED
        assert evidence in result.stdout
        assert json.loads(final_message(result.stdout))["findings"] == [FINDING]
    else:
        expected = {"cancel": JobStatus.CANCELLED, "timeout": JobStatus.TIMED_OUT}
        assert result.status is expected.get(scenario, JobStatus.FAILED)
