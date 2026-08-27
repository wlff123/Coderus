import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from coderus.forge.urls import parse_pull_request_url
from coderus.models import PRReviewTask, Repository


def enqueue_pr_review(
    session: Session,
    url: str,
    source_chat_id: str,
    source_message_id: str,
    source_sender_id: str,
) -> PRReviewTask:
    source_repository, number = parse_pull_request_url(url)
    repository = session.scalar(
        select(Repository).where(
            Repository.provider == source_repository.provider,
            Repository.owner == source_repository.owner,
            Repository.name == source_repository.name,
            Repository.is_enabled.is_(True),
        )
    )
    if repository is None:
        raise ValueError("该 PR 所属仓库未由管理员授权")

    task = PRReviewTask(
        repository=repository,
        pr_number=number,
        pr_url=url,
        status="queued",
        review_key=secrets.token_urlsafe(32),
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        source_sender_open_id=source_sender_id,
    )
    session.add(task)
    session.flush()
    return task
