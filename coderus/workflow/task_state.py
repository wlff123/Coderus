from __future__ import annotations

from collections.abc import Collection, Mapping

from sqlalchemy import update
from sqlalchemy.orm import Session

from coderus.models import Task


def cas_task_status(
    session: Session,
    task_id: int,
    *,
    expected: str | Collection[str],
    new_status: str,
    updates: Mapping[str, object] | None = None,
) -> bool:
    expected_statuses = (expected,) if isinstance(expected, str) else tuple(expected)
    if not expected_statuses:
        raise ValueError("expected task statuses must not be empty")
    result = session.execute(
        update(Task)
        .where(Task.id == task_id, Task.status.in_(expected_statuses))
        .values(status=new_status, **dict(updates or {}))
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
