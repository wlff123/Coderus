from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from coderus.tasks.statuses import TERMINAL_TASK_STATES


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    feishu_open_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repositories: Mapped[list[Repository]] = relationship(back_populates="created_by_user")
    tasks: Mapped[list[Task]] = relationship(back_populates="creator")


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    account_name: Mapped[str] = mapped_column(String(255))
    encrypted_token: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FeishuBotSettings(Base):
    __tablename__ = "feishu_bot_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_feishu_bot_settings_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    app_id: Mapped[str | None] = mapped_column(String(255))
    encrypted_app_secret: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    default_chat_id: Mapped[str | None] = mapped_column(String(255))
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("provider", "owner", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), index=True)
    owner: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    canonical_url: Mapped[str] = mapped_column(String(1000), unique=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    fork_owner: Mapped[str | None] = mapped_column(String(255))
    fork_url: Mapped[str | None] = mapped_column(String(1000))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    sync_status: Mapped[str] = mapped_column(String(30), default="idle")
    sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_cursor_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    setup_commands: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    created_by_user: Mapped[User] = relationship(back_populates="repositories")
    issues: Mapped[list[Issue]] = relationship(back_populates="repository")
    pr_review_tasks: Mapped[list[PRReviewTask]] = relationship(back_populates="repository")


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("repository_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(1000))
    body: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(255))
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(30), index=True)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triage_state: Mapped[str] = mapped_column(String(30), default="discovered", index=True)
    ignored_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    ignored_reason: Mapped[str | None] = mapped_column(Text)
    ignored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    repository: Mapped[Repository] = relationship(back_populates="issues")
    tasks: Mapped[list[Task]] = relationship(back_populates="issue")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(50), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    instructions: Mapped[str] = mapped_column(Text, default="")
    base_commit_sha: Mapped[str | None] = mapped_column(String(64))
    branch_name: Mapped[str | None] = mapped_column(String(255))
    workspace_path: Mapped[str | None] = mapped_column(String(1000))
    fixed_patch_path: Mapped[str | None] = mapped_column(String(1000))
    reviewed_tree_sha: Mapped[str | None] = mapped_column(String(64))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    pr_url: Mapped[str | None] = mapped_column(String(1000))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_state: Mapped[str | None] = mapped_column(String(30))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    issue: Mapped[Issue] = relationship(back_populates="tasks")
    creator: Mapped[User] = relationship(back_populates="tasks")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="task")
    reviews: Mapped[list[Review]] = relationship(back_populates="task")
    pr_feedback: Mapped[list[PRFeedback]] = relationship(back_populates="task")


class FeishuEvent(Base):
    __tablename__ = "feishu_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_id: Mapped[str] = mapped_column(String(255))
    chat_id: Mapped[str] = mapped_column(String(255), index=True)
    chat_type: Mapped[str] = mapped_column(String(30))
    sender_open_id: Mapped[str] = mapped_column(String(255))
    command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    error_summary: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PRReviewTask(Base):
    __tablename__ = "pr_review_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), index=True)
    pr_number: Mapped[int] = mapped_column(Integer)
    pr_url: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    workspace_path: Mapped[str | None] = mapped_column(String(1000))
    source_chat_id: Mapped[str] = mapped_column(String(255))
    source_message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_sender_open_id: Mapped[str] = mapped_column(String(255))
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    comment_url: Mapped[str | None] = mapped_column(String(1000))
    review_key: Mapped[str | None] = mapped_column(String(100))
    claim_token: Mapped[str | None] = mapped_column(String(100))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped[Repository] = relationship(back_populates="pr_review_tasks")


Index(
    "uq_active_task_per_issue",
    Task.issue_id,
    unique=True,
    sqlite_where=Task.status.not_in(TERMINAL_TASK_STATES),
)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    role: Mapped[str] = mapped_column(String(30))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    structured_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_summary: Mapped[str | None] = mapped_column(Text)

    task: Mapped[Task] = relationship(back_populates="agent_runs")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    reviewer_role: Mapped[str] = mapped_column(String(30))
    decision: Mapped[str] = mapped_column(String(30))
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    blocking_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[Task] = relationship(back_populates="reviews")


class PRFeedback(Base):
    __tablename__ = "pr_feedback"
    __table_args__ = (UniqueConstraint("task_id", "provider_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    author: Mapped[str] = mapped_column(String(255))
    author_association: Mapped[str] = mapped_column(String(30))
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(1000))
    path: Mapped[str | None] = mapped_column(String(1000))
    line: Mapped[int | None] = mapped_column(Integer)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[Task] = relationship(back_populates="pr_feedback")
