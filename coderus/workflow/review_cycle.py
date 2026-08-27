"""双 Reviewer 周期：并行执行、结构化解析、Review 持久化与 finding 去重。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from coderus.models import Review, Task
from coderus.runner import AgentRole, Stage
from coderus.workflow.agent_stage import AgentStageExecutor, final_message
from coderus.workflow.prompts import review_prompt
from coderus.workflow.reviewer_result import (
    REVIEWER_CONTRACT_VERSION,
    ReviewerResultError,
    parse_reviewer_result,
)

REVIEWER_SPECS = (
    (Stage.REVIEW_CORRECTNESS, AgentRole.REVIEWER_A, "正确性、回归风险和测试覆盖"),
    (Stage.REVIEW_SECURITY, AgentRole.REVIEWER_B, "安全性、边界条件和可维护性"),
)


def review_result(stdout: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        result = parse_reviewer_result(final_message(stdout).strip())
    except ReviewerResultError:
        return "changes_requested", [
            {"severity": "high", "message": "检视结果不符合版本化契约"}
        ]
    return result.decision, [finding.model_dump() for finding in result.findings]


def deduplicate_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in findings:
        key = json.dumps(finding, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


class ReviewCycle:
    def __init__(
        self,
        *,
        executor: AgentStageExecutor,
        session_factory: Callable[[], Session],
    ) -> None:
        self.executor = executor
        self.sessions = session_factory

    async def run(
        self,
        task: Task,
        workspace: Path,
        developer_report: str,
        claim_token: str,
        cancel_event: asyncio.Event,
    ) -> list[dict[str, Any]]:
        reviewer_tasks: list[asyncio.Task] = []
        async with asyncio.TaskGroup() as group:
            for stage, role, focus in REVIEWER_SPECS:
                reviewer_tasks.append(
                    group.create_task(
                        self.executor.execute(
                            task_id=task.id,
                            status="reviewing",
                            stage=stage,
                            role=role,
                            workspace=workspace,
                            prompt=review_prompt(task, focus, developer_report),
                            claim_token=claim_token,
                            cancel_event=cancel_event,
                        )
                    )
                )
        results = [reviewer_task.result() for reviewer_task in reviewer_tasks]
        findings: list[dict[str, Any]] = []
        for (_, role, _), result in zip(REVIEWER_SPECS, results, strict=True):
            decision, role_findings = review_result(result.stdout)
            self._record_review(task.id, role.value, decision, role_findings)
            if decision != "approve":
                findings.extend(role_findings)
        return deduplicate_findings(findings)

    def _record_review(
        self, task_id: int, role: str, decision: str, findings: list[dict[str, Any]]
    ) -> None:
        with self.sessions() as session:
            session.add(
                Review(
                    task_id=task_id,
                    reviewer_role=role,
                    decision=decision,
                    findings=findings,
                    blocking_count=len(findings) if decision != "approve" else 0,
                    contract_version=REVIEWER_CONTRACT_VERSION,
                )
            )
            session.commit()
