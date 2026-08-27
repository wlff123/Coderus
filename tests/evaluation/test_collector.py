from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from coderus.evaluation.collector import collect_baseline
from coderus.evaluation.models import BaselineSelection, TaskAnnotation
from coderus.models import AgentRun, Issue, Repository, Review, Task, TaskTransition, User

STARTED = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def seed_tasks(session: Session) -> None:
    user = User(username="admin", password_hash="hash", role="admin")
    repository = Repository(
        provider="github",
        owner="acme",
        name="widgets",
        canonical_url="https://github.com/acme/widgets",
        created_by_user=user,
    )
    session.add(repository)
    specs = [
        ("completed", "https://github.com/acme/widgets/pull/1"),
        ("completed", "https://github.com/acme/widgets/pull/2"),
        ("failed", "https://github.com/acme/widgets/pull/3"),  # 有 PR 仍算 pr_created
        ("manual_intervention", None),
        ("manual_intervention", None),
        ("failed", None),
        ("cancelled", None),
        ("closed", None),
        ("dismissed", None),
        ("developer_working", None),  # 运行中 → incomplete
    ]
    for index, (status, pr_url) in enumerate(specs, start=1):
        issue = Issue(
            repository=repository,
            external_id=str(index),
            number=index,
            title=f"issue {index}",
            body="detail",
            state="open",
        )
        task = Task(
            issue=issue,
            creator=user,
            status=status,
            pr_url=pr_url,
            started_at=STARTED,
            finished_at=(
                STARTED + timedelta(minutes=10 * index)
                if status != "developer_working"
                else None
            ),
        )
        session.add(task)
    session.commit()

    first = session.get(Task, 1)
    session.add_all(
        [
            AgentRun(
                task_id=first.id,
                role="developer",
                status="succeeded",
                structured_result={
                    "model_usage": {"request_count": 3, "output_bytes": 100}
                },
            ),
            AgentRun(
                task_id=first.id,
                role="reviewer_a",
                status="succeeded",
                structured_result={
                    "model_usage": {"request_count": 1, "output_bytes": 28}
                },
            ),
            AgentRun(task_id=first.id, role="reviewer_b", status="succeeded"),
            Review(
                task_id=first.id,
                reviewer_role="reviewer_a",
                decision="approve",
                findings=[{"severity": "P2"}, {"severity": "P3"}],
            ),
            TaskTransition(task_id=first.id, from_status="queued", to_status="preparing"),
            TaskTransition(
                task_id=first.id, from_status="preparing", to_status="developer_working"
            ),
            # 旧任务的非法用量结构必须被忽略
            AgentRun(
                task_id=2,
                role="developer",
                status="succeeded",
                structured_result={
                    "model_usage": {"request_count": True, "output_bytes": -1}
                },
            ),
        ]
    )
    session.commit()


def selection_of_all() -> BaselineSelection:
    return BaselineSelection(
        task_keys=tuple(f"RE-{index}" for index in range(1, 11)),
        annotations=(
            TaskAnnotation(
                task_key="RE-1",
                tests_passed=True,
                accepted_without_code_changes=False,
                human_changed_lines=4,
            ),
            TaskAnnotation(task_key="RE-2", tests_passed=False),
        ),
    )


def test_collect_baseline_aggregates_records_and_summary(session: Session) -> None:
    seed_tasks(session)
    fixed_now = datetime(2026, 8, 28, tzinfo=UTC)

    report = collect_baseline(session, selection_of_all(), now=fixed_now)

    assert report.generated_at == fixed_now
    assert report.summary.total == 10
    assert report.summary.pr_created == 3
    assert report.summary.manual_intervention == 2
    assert report.summary.failed == 1
    assert report.summary.cancelled == 1
    assert report.summary.closed == 2  # closed + dismissed
    assert report.summary.incomplete == 1
    assert report.summary.pr_created_rate == pytest.approx(0.3)
    assert report.summary.median_duration_seconds == pytest.approx(3000)
    assert report.summary.verified_test_pass_rate == pytest.approx(0.5)
    assert report.summary.accepted_without_code_changes_rate == pytest.approx(0.0)
    assert report.summary.median_human_changed_lines == pytest.approx(4)

    first = report.records[0]
    assert first.task_key == "RE-1"
    assert first.repository == "acme/widgets"
    assert first.issue_number == 1
    assert first.outcome == "pr_created"
    assert first.duration_seconds == pytest.approx(600)
    assert first.transition_count == 2
    assert first.developer_runs == 1
    assert first.reviewer_runs == 2
    assert first.reviewer_findings == 2
    assert first.model_requests == 4
    assert first.model_output_bytes == 128
    assert first.tests_passed is True
    assert first.human_changed_lines == 4

    # 非法用量结构被忽略，旧任务无数据 → None
    second = report.records[1]
    assert second.model_requests is None
    assert second.model_output_bytes is None
    assert report.records[9].outcome == "incomplete"
    assert report.records[9].duration_seconds is None


def test_collect_baseline_preserves_selection_order(session: Session) -> None:
    seed_tasks(session)
    selection = BaselineSelection(
        task_keys=tuple(f"RE-{index}" for index in range(10, 0, -1))
    )

    report = collect_baseline(session, selection)

    assert [record.task_key for record in report.records] == [
        f"RE-{index}" for index in range(10, 0, -1)
    ]


def test_collect_baseline_rejects_missing_tasks(session: Session) -> None:
    seed_tasks(session)
    selection = BaselineSelection(
        task_keys=(*tuple(f"RE-{index}" for index in range(1, 10)), "RE-404")
    )

    with pytest.raises(ValueError, match="RE-404"):
        collect_baseline(session, selection)
