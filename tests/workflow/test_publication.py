"""TaskPublication：封装顺序、发布对账（publication_key 复用）与凭据缺失拒绝。"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from coderus.forge import ForgeNotConfigured, ForgeRegistry
from coderus.models import Issue, Repository, Task, User
from coderus.workflow.developer_report import DeveloperReport
from coderus.workflow.publication import TaskPublication
from coderus.workflow.task_state import ClaimLost

CLAIM = "claim-token"


class RecordingGit:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def assert_has_changes(self, workspace) -> None:
        self.calls.append("assert_has_changes")

    async def seal(self, workspace, patch_path):
        self.calls.append("seal")
        return SimpleNamespace(patch_path=patch_path, tree_sha="tree-sha")

    async def assert_no_secrets(self, workspace) -> None:
        self.calls.append("assert_no_secrets")

    async def assert_tree(self, workspace, tree_sha) -> None:
        self.calls.append("assert_tree")

    async def commit(self, workspace, title, user, email) -> str:
        self.calls.append("commit")
        return "commit-sha"

    async def assert_committed_tree(self, workspace, commit_sha, tree_sha) -> None:
        self.calls.append("assert_committed_tree")

    async def assert_clean_commit(self, workspace, commit_sha) -> None:
        self.calls.append("assert_clean_commit")


class RecordingForge:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, request):
        self.published.append(dataclasses.asdict(request))
        return SimpleNamespace(url="https://pr", number=1, state="open")


def report() -> DeveloperReport:
    return DeveloperReport(
        problem_description="启动崩溃",
        problem_reproduction="已复现",
        solution="修复空指针",
        change_validation="diff 已检查",
        regression_tests="测试已运行",
        remaining_issues="无已知遗留问题",
    )


def seed_task(engine, *, status: str = "preparing") -> int:
    with Session(engine) as session:
        user = User(username="dev", password_hash="hash", role="admin")
        repository = Repository(
            provider="github",
            owner="octo",
            name="demo",
            canonical_url="https://github.com/octo/demo",
            default_branch="main",
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
        task = Task(issue=issue, creator=user, status=status, claim_token=CLAIM)
        session.add(task)
        session.commit()
        return task.id


def load_task(engine, task_id: int) -> Task:
    with Session(engine) as session:
        task = session.scalar(select(Task).where(Task.id == task_id))
        _ = task.issue.repository
        session.expunge_all()
        return task


def publication(engine, git, forge, tmp_path, transitions=None):
    recorded = transitions if transitions is not None else []

    def transition(task_id: int, status: str, claim: str) -> None:
        recorded.append(status)
        with Session(engine) as session:
            session.execute(
                update(Task).where(Task.id == task_id).values(status=status)
            )
            session.commit()

    return TaskPublication(
        session_factory=lambda: Session(engine),
        workspace_git=git,
        forges=ForgeRegistry({"github": forge}),
        artifacts_root=tmp_path / "artifacts",
        git_user_name="Coderus Bot",
        git_user_email="coderus@example.com",
        transition=transition,
    )


def publication_key_from(body: str) -> str:
    return re.search(r"coderus-publication:(\S+) -->", body).group(1)


def test_finalize_runs_sealing_steps_in_order(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    git = RecordingGit()
    forge = RecordingForge()
    transitions: list[str] = []
    pub = publication(engine, git, forge, tmp_path, transitions)
    task = load_task(engine, task_id)

    published = asyncio.run(
        pub.finalize(
            task,
            tmp_path,
            "coderus/issue-1-1",
            [report()],
            patch_name="fixed.patch",
            commit_title="Fix #1: crash",
            claim_token=CLAIM,
        )
    )

    assert git.calls == [
        "assert_has_changes",
        "seal",
        "assert_no_secrets",
        "assert_tree",
        "commit",
        "assert_committed_tree",
    ]
    assert transitions == ["sealing"]
    assert published.url == "https://pr"
    assert forge.published[0]["branch"] == "coderus/issue-1-1"
    assert forge.published[0]["default_branch"] == "main"
    assert "Resolves #1" in forge.published[0]["body"]
    with Session(engine) as session:
        row = session.get(Task, task_id)
        assert row.commit_sha == "commit-sha"
        assert row.reviewed_tree_sha == "tree-sha"
        assert row.status == "publishing"
        assert row.publication_key is not None


def test_retry_reuses_publication_key_branch_and_commit(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    git = RecordingGit()
    forge = RecordingForge()
    pub = publication(engine, git, forge, tmp_path)
    with Session(engine) as session:
        session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(commit_sha="commit-sha", branch_name="coderus/issue-1-1")
        )
        session.commit()
    task = load_task(engine, task_id)

    first = asyncio.run(pub.publish_existing(task, tmp_path, [report()], CLAIM))
    with Session(engine) as session:
        session.execute(
            update(Task).where(Task.id == task_id).values(status="preparing")
        )
        session.commit()
    second = asyncio.run(pub.publish_existing(task, tmp_path, [report()], CLAIM))

    assert first.number == second.number == 1
    keys = [publication_key_from(call["body"]) for call in forge.published]
    assert keys[0] == keys[1]
    branches = [call["branch"] for call in forge.published]
    assert branches == ["coderus/issue-1-1", "coderus/issue-1-1"]
    assert git.calls == ["assert_clean_commit", "assert_clean_commit"]


def test_publish_rejects_when_claim_is_lost(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    pub = publication(engine, RecordingGit(), RecordingForge(), tmp_path)
    task = load_task(engine, task_id)

    with pytest.raises(ClaimLost):
        asyncio.run(pub.publish(task, tmp_path, "branch", [report()], "wrong-claim"))


def test_publish_requires_configured_forge(engine, tmp_path) -> None:
    task_id = seed_task(engine)
    pub = TaskPublication(
        session_factory=lambda: Session(engine),
        workspace_git=RecordingGit(),
        forges=ForgeRegistry(),
        artifacts_root=tmp_path,
        git_user_name="bot",
        git_user_email="bot@example.com",
        transition=lambda *args: None,
    )
    task = load_task(engine, task_id)

    with pytest.raises(ForgeNotConfigured):
        asyncio.run(pub.publish(task, tmp_path, "branch", [report()], CLAIM))
