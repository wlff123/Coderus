from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coderus.evaluation.io import load_selection, write_report
from coderus.evaluation.models import (
    BaselineReport,
    BaselineSelection,
    BaselineSummary,
    TaskBaseline,
)


@pytest.fixture
def report() -> BaselineReport:
    record = TaskBaseline(
        task_key="RE-1",
        provider="github",
        repository="acme/widgets",
        issue_number=1,
        status="completed",
        outcome="pr_created",
        duration_seconds=600.0,
        transition_count=2,
        developer_runs=1,
        reviewer_runs=2,
        reviewer_findings=2,
        model_requests=4,
        model_output_bytes=128,
        tests_passed=True,
        accepted_without_code_changes=None,
        human_changed_lines=None,
    )
    return BaselineReport(
        generated_at=datetime(2026, 8, 28, tzinfo=UTC),
        records=(record,),
        summary=BaselineSummary(
            total=1,
            pr_created=1,
            manual_intervention=0,
            failed=0,
            cancelled=0,
            closed=0,
            incomplete=0,
            pr_created_rate=1.0,
            median_duration_seconds=600.0,
            verified_test_pass_rate=1.0,
            accepted_without_code_changes_rate=None,
            median_human_changed_lines=None,
        ),
    )


def test_write_report_replaces_destination_atomically(
    tmp_path: Path, report: BaselineReport
) -> None:
    destination = tmp_path / "baseline.json"
    destination.write_text("old", encoding="utf-8")

    write_report(destination, report)

    saved = BaselineReport.model_validate_json(destination.read_text("utf-8"))
    assert saved == report
    assert list(tmp_path.glob(".baseline.json.*.tmp")) == []
    assert destination.read_text("utf-8").endswith("\n")


def test_failed_replace_keeps_old_file_and_cleans_up(
    tmp_path: Path, report: BaselineReport, monkeypatch
) -> None:
    destination = tmp_path / "baseline.json"
    destination.write_text("old", encoding="utf-8")

    def broken_replace(source: str, target: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        write_report(destination, report)

    assert destination.read_text("utf-8") == "old"
    assert list(tmp_path.glob(".baseline.json.*.tmp")) == []


def test_serialized_report_contains_no_sensitive_fields(
    tmp_path: Path, report: BaselineReport
) -> None:
    destination = tmp_path / "baseline.json"
    write_report(destination, report)

    serialized = destination.read_text("utf-8").lower()
    for forbidden in ("token", "api_key", "workspace_path", "stdout", "issue_body"):
        assert forbidden not in serialized


def test_load_selection_round_trips(tmp_path: Path) -> None:
    selection = BaselineSelection(
        task_keys=tuple(f"RE-{index}" for index in range(1, 11))
    )
    path = tmp_path / "selection.json"
    path.write_text(selection.model_dump_json(), encoding="utf-8")

    assert load_selection(path) == selection


@pytest.mark.parametrize(
    "content", [None, "not json", '{"contract_version": 2, "task_keys": []}']
)
def test_load_selection_rejects_bad_input_without_echoing(
    tmp_path: Path, content: str | None
) -> None:
    path = tmp_path / "selection.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="^invalid evaluation selection$"):
        load_selection(path)
