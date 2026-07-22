from __future__ import annotations

import secrets
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased

from coderus.models import Task, TaskTransition
from coderus.tasks.statuses import RUNNING_TASK_STATES

TASK_CONTRACT_VERSION = 1


def cas_task_status(
    session: Session,
    task_id: int,
    *,
    expected: str | Collection[str],
    new_status: str,
    updates: Mapping[str, object] | None = None,
    claim_token: str | None = None,
    actor: str = "system",
) -> bool:
    expected_statuses = (expected,) if isinstance(expected, str) else tuple(expected)
    if not expected_statuses:
        raise ValueError("expected task statuses must not be empty")

    current_statement = select(Task.status, Task.contract_version).where(
        Task.id == task_id,
        Task.status.in_(expected_statuses),
    )
    if claim_token is not None:
        current_statement = current_statement.where(Task.claim_token == claim_token)
    current = session.execute(current_statement).one_or_none()
    if current is None:
        return False

    statement = update(Task).where(Task.id == task_id, Task.status == current.status)
    if claim_token is not None:
        statement = statement.where(Task.claim_token == claim_token)
    result = session.execute(
        statement
        .values(status=new_status, **dict(updates or {}))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    session.add(
        TaskTransition(
            task_id=task_id,
            from_status=current.status,
            to_status=new_status,
            actor=actor,
            contract_version=current.contract_version or TASK_CONTRACT_VERSION,
        )
    )
    return True


def claim_queued_task(
    session: Session,
    task_id: int,
    *,
    global_limit: int,
    per_user_limit: int,
    lease_seconds: float,
) -> str | None:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    global_tasks = aliased(Task)
    user_tasks = aliased(Task)
    global_running = (
        select(func.count())
        .select_from(global_tasks)
        .where(global_tasks.status.in_(RUNNING_TASK_STATES))
        .scalar_subquery()
    )
    user_running = (
        select(func.count())
        .select_from(user_tasks)
        .where(
            user_tasks.status.in_(RUNNING_TASK_STATES),
            user_tasks.created_by == Task.created_by,
        )
        .correlate(Task)
        .scalar_subquery()
    )
    result = session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            Task.status == "queued",
            global_running < global_limit,
            user_running < per_user_limit,
        )
        .values(
            status="preparing",
            claim_token=token,
            claim_expires_at=now + timedelta(seconds=lease_seconds),
            started_at=func.coalesce(Task.started_at, now),
            finished_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return None
    contract_version = session.scalar(
        select(Task.contract_version).where(Task.id == task_id)
    )
    session.add(
        TaskTransition(
            task_id=task_id,
            from_status="queued",
            to_status="preparing",
            actor="scheduler",
            contract_version=contract_version or TASK_CONTRACT_VERSION,
        )
    )
    return token
