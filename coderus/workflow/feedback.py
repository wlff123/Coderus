from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from coderus.forge.models import PRFeedbackItem
from coderus.models import PRFeedback


def upsert_pr_feedback(
    session: Session,
    *,
    task_id: int,
    provider: str,
    item: PRFeedbackItem,
) -> None:
    provider_id = f"{provider}:{item.provider_id}"
    rows = session.scalars(
        select(PRFeedback).where(
            PRFeedback.task_id == task_id,
            PRFeedback.provider_id.in_({provider_id, item.provider_id}),
        )
    ).all()
    prefixed = next((row for row in rows if row.provider_id == provider_id), None)
    legacy = next(
        (
            row
            for row in rows
            if row.provider_id == item.provider_id and row is not prefixed
        ),
        None,
    )
    if prefixed is None and legacy is not None:
        legacy.provider_id = provider_id
        prefixed = legacy
    elif prefixed is not None and legacy is not None:
        selected = [value for value in (prefixed.selected_at, legacy.selected_at) if value]
        processed = [
            value for value in (prefixed.processed_at, legacy.processed_at) if value
        ]
        prefixed.selected_at = min(selected) if selected else None
        prefixed.processed_at = min(processed) if processed else None
        session.delete(legacy)
    session.flush()

    statement = sqlite_insert(PRFeedback).values(
        task_id=task_id,
        provider_id=provider_id,
        kind=item.kind,
        author=item.author,
        author_association=item.author_association,
        body=item.body,
        url=item.url,
        path=item.path,
        line=item.line,
        created_at=datetime.now(UTC),
    )
    excluded = statement.excluded
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[PRFeedback.task_id, PRFeedback.provider_id],
            set_={
                "kind": excluded.kind,
                "author": excluded.author,
                "author_association": excluded.author_association,
                "body": excluded.body,
                "url": excluded.url,
                "path": excluded.path,
                "line": excluded.line,
            },
        )
    )
