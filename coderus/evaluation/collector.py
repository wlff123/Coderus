"""从历史任务数据只读收集评测基线。

只执行 SELECT，不改写任何行；结果按选择文件顺序输出，未知指标为 None。
"""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from coderus.evaluation.models import (
    BaselineReport,
    BaselineSelection,
    BaselineSummary,
    TaskAnnotation,
    TaskBaseline,
    TaskOutcome,
)
from coderus.models import Issue, Task

_STATUS_OUTCOMES: dict[str, TaskOutcome] = {
    "manual_intervention": "manual_intervention",
    "failed": "failed",
    "cancelled": "cancelled",
    "closed": "closed",
    "dismissed": "closed",
}


def collect_baseline(
    session: Session,
    selection: BaselineSelection,
    *,
    now: datetime | None = None,
) -> BaselineReport:
    task_ids = [int(key[3:]) for key in selection.task_keys]
    tasks = {
        task.id: task
        for task in session.scalars(
            select(Task)
            .where(Task.id.in_(task_ids))
            .options(
                selectinload(Task.issue).selectinload(Issue.repository),
                selectinload(Task.agent_runs),
                selectinload(Task.reviews),
                selectinload(Task.transitions),
            )
        )
    }
    missing = [
        key
        for key, task_id in zip(selection.task_keys, task_ids, strict=True)
        if task_id not in tasks
    ]
    if missing:
        raise ValueError(f"selection references missing tasks: {', '.join(missing)}")

    annotations = {item.task_key: item for item in selection.annotations}
    records = tuple(
        _baseline_record(key, tasks[task_id], annotations.get(key))
        for key, task_id in zip(selection.task_keys, task_ids, strict=True)
    )
    return BaselineReport(
        generated_at=now or datetime.now(UTC),
        records=records,
        summary=_summarize(records),
    )


def _baseline_record(
    task_key: str, task: Task, annotation: TaskAnnotation | None
) -> TaskBaseline:
    repository = task.issue.repository
    duration = None
    if task.started_at is not None and task.finished_at is not None:
        duration = (task.finished_at - task.started_at).total_seconds()
    model_requests, model_output_bytes = _model_usage_totals(task)
    return TaskBaseline(
        task_key=task_key,
        provider=repository.provider,
        repository=f"{repository.owner}/{repository.name}",
        issue_number=task.issue.number,
        status=task.status,
        outcome=(
            "pr_created"
            if task.pr_url
            else _STATUS_OUTCOMES.get(task.status, "incomplete")
        ),
        duration_seconds=duration,
        transition_count=len(task.transitions),
        developer_runs=sum(run.role == "developer" for run in task.agent_runs),
        reviewer_runs=sum(run.role.startswith("reviewer") for run in task.agent_runs),
        reviewer_findings=sum(len(review.findings or []) for review in task.reviews),
        model_requests=model_requests,
        model_output_bytes=model_output_bytes,
        tests_passed=annotation.tests_passed if annotation else None,
        accepted_without_code_changes=(
            annotation.accepted_without_code_changes if annotation else None
        ),
        human_changed_lines=annotation.human_changed_lines if annotation else None,
    )


def _model_usage_totals(task: Task) -> tuple[int | None, int | None]:
    requests = output_bytes = 0
    seen = False
    for run in task.agent_runs:
        usage = (run.structured_result or {}).get("model_usage")
        if not isinstance(usage, dict):
            continue
        run_requests = usage.get("request_count")
        run_output = usage.get("output_bytes")
        if not _is_count(run_requests) or not _is_count(run_output):
            continue
        seen = True
        requests += run_requests
        output_bytes += run_output
    return (requests, output_bytes) if seen else (None, None)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _summarize(records: tuple[TaskBaseline, ...]) -> BaselineSummary:
    outcomes = [record.outcome for record in records]
    durations = [
        record.duration_seconds
        for record in records
        if record.duration_seconds is not None
    ]
    tests = [record.tests_passed for record in records if record.tests_passed is not None]
    accepted = [
        record.accepted_without_code_changes
        for record in records
        if record.accepted_without_code_changes is not None
    ]
    changed_lines = [
        record.human_changed_lines
        for record in records
        if record.human_changed_lines is not None
    ]
    return BaselineSummary(
        total=len(records),
        pr_created=outcomes.count("pr_created"),
        manual_intervention=outcomes.count("manual_intervention"),
        failed=outcomes.count("failed"),
        cancelled=outcomes.count("cancelled"),
        closed=outcomes.count("closed"),
        incomplete=outcomes.count("incomplete"),
        pr_created_rate=outcomes.count("pr_created") / len(records),
        median_duration_seconds=median(durations) if durations else None,
        verified_test_pass_rate=sum(tests) / len(tests) if tests else None,
        accepted_without_code_changes_rate=(
            sum(accepted) / len(accepted) if accepted else None
        ),
        median_human_changed_lines=(
            float(median(changed_lines)) if changed_lines else None
        ),
    )
