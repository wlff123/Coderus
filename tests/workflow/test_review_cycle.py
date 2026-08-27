"""ReviewCycle：并行执行、每角色一条 Review、finding 去重且顺序稳定。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.models import Issue, Repository, Review, Task, User
from coderus.runner import AgentRole, JobResult, JobStatus
from coderus.workflow.review_cycle import ReviewCycle, deduplicate_findings

FINDING_A = {"severity": "high", "message": "缺少空值检查"}
FINDING_SHARED = {"severity": "medium", "message": "错误信息不清晰"}
FINDING_B = {"severity": "low", "message": "命名不一致"}


def reviewer_stdout(decision: str, findings: list[dict]) -> str:
    payload = {"contract_version": 1, "decision": decision, "findings": findings}
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(payload, ensure_ascii=False),
            },
        }
    )


class ParallelFakeExecutor:
    """execute 必须并发运行：每个调用等到两个调用都已开始才返回。"""

    def __init__(self, stdout_by_role: dict[str, str]) -> None:
        self.stdout_by_role = stdout_by_role
        self.started: dict[str, asyncio.Event] = {
            role: asyncio.Event() for role in stdout_by_role
        }
        self.calls: list[dict] = []

    async def execute(self, **kwargs) -> JobResult:
        self.calls.append(kwargs)
        role = kwargs["role"].value
        self.started[role].set()
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in self.started.values())),
            timeout=5,
        )
        return JobResult(
            job_id=f"job-{role}",
            status=JobStatus.SUCCEEDED,
            exit_code=0,
            stdout=self.stdout_by_role[role],
            stderr="",
            output_truncated=False,
            duration_seconds=0.1,
        )


def seed_task(engine) -> int:
    with Session(engine) as session:
        user = User(username="dev", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            created_by_user=user,
        )
        issue = Issue(
            repository=repository,
            external_id="1",
            number=1,
            title="crash",
            body="detail",
            state="open",
            source_url="https://github.com/octo/demo/issues/1",
        )
        issue.triage_state = "dispatched"
        task = Task(issue=issue, creator=user, status="reviewing")
        session.add(task)
        session.commit()
        return task.id


def fake_task(task_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        instructions="",
        issue=SimpleNamespace(
            number=1,
            title="crash",
            body="detail",
            source_url="https://github.com/octo/demo/issues/1",
        ),
    )


def test_reviewers_run_in_parallel_and_findings_are_deduplicated(
    engine, tmp_path
) -> None:
    task_id = seed_task(engine)
    executor = ParallelFakeExecutor(
        {
            "reviewer_a": reviewer_stdout(
                "changes_requested", [FINDING_A, FINDING_SHARED]
            ),
            "reviewer_b": reviewer_stdout(
                "changes_requested", [FINDING_SHARED, FINDING_B]
            ),
        }
    )
    cycle = ReviewCycle(executor=executor, session_factory=lambda: Session(engine))

    findings = asyncio.run(
        cycle.run(fake_task(task_id), tmp_path, "开发报告", "claim", asyncio.Event())
    )

    assert findings == [FINDING_A, FINDING_SHARED, FINDING_B]
    assert [call["role"] for call in executor.calls] == [
        AgentRole.REVIEWER_A,
        AgentRole.REVIEWER_B,
    ]
    assert all(call["status"] == "reviewing" for call in executor.calls)
    with Session(engine) as session:
        reviews = session.scalars(select(Review).order_by(Review.id)).all()
        assert [review.reviewer_role for review in reviews] == [
            "reviewer_a",
            "reviewer_b",
        ]
        assert all(review.decision == "changes_requested" for review in reviews)
        assert reviews[0].blocking_count == 2


def test_approval_records_review_without_findings(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    executor = ParallelFakeExecutor(
        {
            "reviewer_a": reviewer_stdout("approve", []),
            "reviewer_b": reviewer_stdout("approve", []),
        }
    )
    cycle = ReviewCycle(executor=executor, session_factory=lambda: Session(engine))

    findings = asyncio.run(
        cycle.run(fake_task(task_id), tmp_path, "开发报告", "claim", asyncio.Event())
    )

    assert findings == []
    with Session(engine) as session:
        reviews = session.scalars(select(Review)).all()
        assert len(reviews) == 2
        assert all(review.blocking_count == 0 for review in reviews)


def test_invalid_reviewer_output_degrades_to_blocking_finding(
    engine, tmp_path
) -> None:
    task_id = seed_task(engine)
    executor = ParallelFakeExecutor(
        {
            "reviewer_a": "不是 JSON 的输出",
            "reviewer_b": reviewer_stdout("approve", []),
        }
    )
    cycle = ReviewCycle(executor=executor, session_factory=lambda: Session(engine))

    findings = asyncio.run(
        cycle.run(fake_task(task_id), tmp_path, "开发报告", "claim", asyncio.Event())
    )

    assert findings == [{"severity": "high", "message": "检视结果不符合版本化契约"}]


def test_deduplicate_keeps_first_occurrence_order() -> None:
    assert deduplicate_findings([FINDING_A, FINDING_SHARED, FINDING_A]) == [
        FINDING_A,
        FINDING_SHARED,
    ]
