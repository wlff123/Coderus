import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from coderus.db import create_session_factory
from coderus.forge import ForgeRegistry
from coderus.models import AgentRun, Issue, PRFeedback, Repository, Review, Task, User
from coderus.runner import JobResult, JobStatus
from coderus.workflow.developer_report import developer_report_schema_path
from coderus.workflow.orchestrator import TaskOrchestrator

DEVELOPER_REPORT = {
    "problem_description": "Issue 描述的边界输入会触发错误行为。",
    "problem_reproduction": "已按 Issue 步骤复现，并确认修复前测试失败。",
    "solution": "初次修改方案：对边界输入增加最小处理。",
    "change_validation": "已检查完整差异，确认修改范围仅覆盖相关逻辑。",
    "regression_tests": "已运行相关测试，结果为三项通过。",
    "remaining_issues": "无已知遗留问题。",
}


class FakeGit:
    def __init__(self, workspace: Path, *, has_changes: bool = True) -> None:
        self.workspace = workspace
        self.has_changes = has_changes
        self.committed = False
        self.tree_verified = False

    async def prepare(self, task_id: int, repository_url: str, default_branch: str, branch: str):
        self.workspace.mkdir(parents=True)
        return SimpleNamespace(workspace=self.workspace, base_commit_sha="a" * 40, branch=branch)

    async def seal(self, workspace: Path, patch_path: Path):
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text("diff --git a/a b/a\n", encoding="utf-8")
        return SimpleNamespace(patch_path=patch_path, tree_sha="b" * 40)

    async def assert_has_changes(self, workspace: Path) -> None:
        if not self.has_changes:
            raise ValueError("Codex did not produce any code changes")

    async def assert_no_secrets(self, workspace: Path) -> None:
        return None

    async def assert_branch(self, workspace: Path, branch: str) -> None:
        assert branch == "coderus/issue-1-1"

    async def assert_clean_commit(self, workspace: Path, commit_sha: str) -> None:
        assert commit_sha == "c" * 40

    async def commit(self, workspace: Path, title: str, user_name: str, user_email: str) -> str:
        self.committed = True
        return "c" * 40

    async def assert_tree(self, workspace: Path, expected_tree_sha: str) -> None:
        assert expected_tree_sha == "b" * 40
        self.tree_verified = True

    async def assert_committed_tree(
        self, workspace: Path, commit_sha: str, expected_tree_sha: str
    ) -> None:
        assert commit_sha == "c" * 40
        assert expected_tree_sha == "b" * 40


class FakeRunner:
    def __init__(
        self, *, reviewer_decision: str = "approve", invalid_report: bool = False
    ) -> None:
        self.specs = []
        self.reviewer_decision = reviewer_decision
        self.invalid_report = invalid_report

    async def run(self, spec, *, cancel_event=None):
        self.specs.append(spec)
        if "reviewer" in spec.role.value:
            message = json.dumps(
                {
                    "decision": self.reviewer_decision,
                    "findings": (
                        [{"severity": "medium", "message": "targeted tests are missing"}]
                        if self.reviewer_decision == "changes_requested"
                        else []
                    ),
                }
            )
        else:
            developer_attempt = sum(
                previous.role.value == "developer" for previous in self.specs
            )
            report = DEVELOPER_REPORT | {
                "solution": (
                    "最终修改方案：已集中处理检视意见并保持最小修改。"
                    if developer_attempt > 1
                    else DEVELOPER_REPORT["solution"]
                )
            }
            if self.invalid_report:
                report.pop("remaining_issues")
            message = json.dumps(report, ensure_ascii=False)
        stdout = json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}}
        )
        return JobResult(
            job_id=spec.job_id,
            status=JobStatus.SUCCEEDED,
            exit_code=0,
            stdout=stdout,
            stderr="",
            output_truncated=False,
            duration_seconds=0.01,
        )


class FakePublisher:
    def __init__(self) -> None:
        self.calls = []

    async def publish(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(url="https://github.com/octo/demo/pull/9", number=9, state="open")


class FakeGitCodeForge(FakePublisher):
    async def publish(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            url="https://gitcode.com/open/demo/pulls/12",
            number=12,
            state="open",
        )


class FakeNotifier:
    def __init__(self) -> None:
        self.calls = []

    async def notify(self, **kwargs) -> None:
        self.calls.append(kwargs)


class FailingNotifier:
    async def notify(self, **kwargs) -> None:
        raise RuntimeError("secret notification detail")


class FakeBroker:
    def __init__(self) -> None:
        self.revoked = []
        self.issued = []

    def issue(self, *, task_id: str, stage: str, ttl_seconds: float) -> str:
        assert ttl_seconds > 0
        self.issued.append((task_id, stage))
        return "short-lived-token"

    def revoke(self, token: str) -> bool:
        self.revoked.append(token)
        return True


def create_task(session, *, provider: str = "github") -> Task:
    host = "github.com" if provider == "github" else "gitcode.com"
    owner = "octo" if provider == "github" else "open"
    user = User(username="admin", password_hash="hash", role="admin")
    repository = Repository(
        provider=provider,
        owner=owner,
        name="demo",
        canonical_url=f"https://{host}/{owner}/demo",
        default_branch="main",
        created_by_user=user,
    )
    issue = Issue(
        repository=repository,
        external_id="1",
        number=1,
        title="Fix broken behavior",
        body="Steps to reproduce",
        state="open",
        source_url=f"https://{host}/{owner}/demo/issues/1",
        triage_state="dispatched",
    )
    task = Task(issue=issue, creator=user, status="queued")
    session.add(task)
    session.commit()
    return task


def add_developer_report(session, task_id: int) -> None:
    session.add(
        AgentRun(
            task_id=task_id,
            role="developer",
            attempt=1,
            status="succeeded",
            structured_result={"developer_report": DEVELOPER_REPORT},
        )
    )


def make_orchestrator(sessions, tmp_path, runner, publisher=None, **kwargs):
    return TaskOrchestrator(
        session_factory=sessions,
        runner=runner,
        workspace_git=FakeGit(tmp_path / "workspaces" / "task-1"),
        forges=ForgeRegistry({"github": publisher or FakePublisher()}),
        artifacts_root=tmp_path / "artifacts",
        git_user_name="Coderus Bot",
        git_user_email="bot@example.com",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_orchestrator_runs_developer_and_two_reviewers_once(engine, tmp_path: Path) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session).id
    runner = FakeRunner()
    publisher = FakePublisher()
    notifier = FakeNotifier()
    broker = FakeBroker()
    orchestrator = make_orchestrator(
        sessions,
        tmp_path,
        runner,
        publisher,
        notifier=notifier,
        credential_broker=broker,
    )

    await orchestrator.run(task_id)

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "awaiting_human_review"
        assert task.pr_url == "https://github.com/octo/demo/pull/9"
    assert [spec.role.value for spec in runner.specs] == [
        "developer",
        "reviewer_a",
        "reviewer_b",
    ]
    assert "先探索代码并复现" in runner.specs[0].prompt
    assert runner.specs[0].output_schema == developer_report_schema_path()
    assert runner.specs[1].output_schema is None
    assert runner.specs[2].output_schema is None
    for heading in (
        "问题描述",
        "问题复现",
        "修改方案",
        "修改验证",
        "测试回归",
        "遗留问题",
    ):
        assert heading in runner.specs[0].prompt
    assert "按顺序执行" in runner.specs[0].prompt
    assert "未执行测试不得声称通过" in runner.specs[0].prompt
    assert "最终只输出符合 Schema 的 JSON" in runner.specs[0].prompt
    assert notifier.calls[0]["pr_url"] == "https://github.com/octo/demo/pull/9"
    assert notifier.calls[0]["database_task_id"] == task_id
    assert broker.issued == [
        (f"task-{task_id}", "develop"),
        (f"task-{task_id}", "review_correctness"),
        (f"task-{task_id}", "review_security"),
    ]
    assert len(broker.revoked) == len(runner.specs)
    with sessions() as session:
        run = session.scalar(
            select(AgentRun).where(AgentRun.task_id == task_id, AgentRun.role == "developer")
        )
        assert run.structured_result["developer_report"] == DEVELOPER_REPORT


@pytest.mark.asyncio
async def test_gitcode_task_publishes_through_provider_forge_and_persists_pr(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session, provider="gitcode").id
    forge = FakeGitCodeForge()
    orchestrator = TaskOrchestrator(
        session_factory=sessions,
        runner=FakeRunner(),
        workspace_git=FakeGit(tmp_path / "workspaces" / "task-1"),
        forges=ForgeRegistry({"gitcode": forge}),
        artifacts_root=tmp_path / "artifacts",
        git_user_name="Coderus Bot",
        git_user_email="bot@example.com",
    )

    await orchestrator.run(task_id)

    assert len(forge.calls) == 1
    assert forge.calls[0]["upstream_owner"] == "open"
    assert forge.calls[0]["repository_name"] == "demo"
    with sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "awaiting_human_review"
        assert task.pr_url == "https://gitcode.com/open/demo/pulls/12"
        assert task.pr_number == 12
        assert task.pr_state == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitcode"])
async def test_orchestrator_publishes_through_the_task_repository_forge(
    engine, tmp_path: Path, provider: str
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session, provider=provider).id
    github = FakePublisher()
    gitcode = FakeGitCodeForge()
    orchestrator = TaskOrchestrator(
        session_factory=sessions,
        runner=FakeRunner(),
        workspace_git=FakeGit(tmp_path / "workspaces" / "task-1"),
        forges=ForgeRegistry({"github": github, "gitcode": gitcode}),
        artifacts_root=tmp_path / "artifacts",
        git_user_name="Coderus Bot",
        git_user_email="bot@example.com",
    )

    await orchestrator.run(task_id)

    assert len(github.calls) == (provider == "github")
    assert len(gitcode.calls) == (provider == "gitcode")


@pytest.mark.asyncio
async def test_unconfigured_task_provider_keeps_publishable_workspace_and_commit(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session, provider="gitcode").id
    workspace = tmp_path / "workspaces" / "task-1"
    orchestrator = TaskOrchestrator(
        session_factory=sessions,
        runner=FakeRunner(),
        workspace_git=FakeGit(workspace),
        forges=ForgeRegistry({"github": FakePublisher()}),
        artifacts_root=tmp_path / "artifacts",
        git_user_name="Coderus Bot",
        git_user_email="bot@example.com",
    )

    await orchestrator.run(task_id)

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "failed"
        assert task.failure_code == "ForgeNotConfigured"
        assert task.workspace_path == str(workspace)
        assert task.branch_name == "coderus/issue-1-1"
        assert task.commit_sha == "c" * 40


@pytest.mark.asyncio
async def test_notification_failure_does_not_change_published_task_state(
    engine,
    tmp_path: Path,
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session).id
    orchestrator = make_orchestrator(
        sessions,
        tmp_path,
        FakeRunner(),
        FakePublisher(),
        notifier=FailingNotifier(),
    )

    await orchestrator.run(task_id)

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "awaiting_human_review"
        assert task.pr_url == "https://github.com/octo/demo/pull/9"
        assert task.failure_summary == "飞书通知失败：RuntimeError"
        assert "secret notification detail" not in task.failure_summary


@pytest.mark.asyncio
async def test_review_findings_get_one_revision_then_publish_pr(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session).id
    runner = FakeRunner(reviewer_decision="changes_requested")
    publisher = FakePublisher()
    orchestrator = make_orchestrator(sessions, tmp_path, runner, publisher)

    await orchestrator.run(task_id)

    assert [spec.role.value for spec in runner.specs] == [
        "developer",
        "reviewer_a",
        "reviewer_b",
        "developer",
    ]
    revision = runner.specs[-1]
    assert "targeted tests are missing" in revision.prompt
    assert runner.specs[0].output_schema == developer_report_schema_path()
    assert revision.output_schema == developer_report_schema_path()
    assert publisher.calls
    body = publisher.calls[0]["body"]
    assert "最终修改方案" in body
    assert "初次修改方案" not in body
    assert "targeted tests are missing" not in body
    assert "首轮检视意见" not in body
    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "awaiting_human_review"
        assert task.failure_code is None


@pytest.mark.asyncio
async def test_invalid_developer_report_requires_manual_intervention(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session).id
    runner = FakeRunner(invalid_report=True)
    publisher = FakePublisher()
    orchestrator = make_orchestrator(sessions, tmp_path, runner, publisher)

    await orchestrator.run(task_id)

    assert [spec.role.value for spec in runner.specs] == ["developer"]
    assert publisher.calls == []
    with sessions() as session:
        task = session.get(Task, task_id)
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        assert task.status == "manual_intervention"
        assert task.failure_code == "developer_report_invalid"
        assert run.status == "failed"
        assert "remaining_issues" in run.error_summary


@pytest.mark.asyncio
async def test_orchestrator_stops_before_review_when_developer_makes_no_changes(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session).id
    runner = FakeRunner()
    git = FakeGit(tmp_path / "workspaces" / "task-1", has_changes=False)
    orchestrator = TaskOrchestrator(
        session_factory=sessions,
        runner=runner,
        workspace_git=git,
        forges=ForgeRegistry({"github": FakePublisher()}),
        artifacts_root=tmp_path / "artifacts",
        git_user_name="bot",
        git_user_email="bot@example.com",
    )

    await orchestrator.run(task_id)

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "failed"
        assert task.failure_summary == "Codex did not produce any code changes"
    assert [spec.role.value for spec in runner.specs] == ["developer"]


@pytest.mark.asyncio
async def test_selected_pr_feedback_runs_one_developer_and_pushes_same_pr(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    workspace = tmp_path / "workspaces" / "task-1"
    workspace.mkdir(parents=True)
    with sessions() as session:
        task = create_task(session)
        task.status = "queued"
        task.failure_code = "pr_feedback_revision"
        task.workspace_path = str(workspace)
        task.branch_name = "coderus/issue-1-1"
        task.base_commit_sha = "a" * 40
        task.pr_url = "https://github.com/octo/demo/pull/9"
        task.pr_number = 9
        session.add(
            PRFeedback(
                task_id=task.id,
                provider_id="review_comment:1",
                kind="review_comment",
                author="maintainer",
                author_association="MEMBER",
                body="handle None here",
                url="https://github.com/octo/demo/pull/9#discussion_r1",
                selected_at=task.created_at,
            )
        )
        session.commit()
        task_id = task.id
    runner = FakeRunner()
    publisher = FakePublisher()
    orchestrator = make_orchestrator(sessions, tmp_path, runner, publisher)

    await orchestrator.run(task_id)

    assert [spec.role.value for spec in runner.specs] == ["developer"]
    assert "handle None here" in runner.specs[0].prompt
    assert len(publisher.calls) == 1
    assert publisher.calls[0]["branch"] == "coderus/issue-1-1"
    with sessions() as session:
        task = session.get(Task, task_id)
        feedback = session.get(PRFeedback, 1)
        assert task.status == "awaiting_human_review"
        assert task.pr_number == 9
        assert feedback.processed_at is not None


@pytest.mark.asyncio
async def test_publish_completion_does_not_overwrite_concurrent_cancellation(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task_id = create_task(session).id

    class CancellingPublisher(FakePublisher):
        async def publish(self, **kwargs):
            with sessions() as session:
                task = session.get(Task, task_id)
                task.status = "cancelling"
                session.commit()
            return await super().publish(**kwargs)

    orchestrator = make_orchestrator(
        sessions, tmp_path, FakeRunner(), CancellingPublisher()
    )

    await orchestrator.run(task_id)

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task.status == "cancelled"
        assert task.pr_url is None


def test_feedback_processing_marks_only_the_original_snapshot(engine, tmp_path: Path) -> None:
    sessions = create_session_factory(engine)
    with sessions() as session:
        task = create_task(session)
        first = PRFeedback(
            task_id=task.id,
            provider_id="comment:first",
            kind="issue_comment",
            author="maintainer",
            author_association="MEMBER",
            body="first",
            url="https://github.com/octo/demo/pull/9#first",
            selected_at=task.created_at,
        )
        session.add(first)
        session.commit()
        task_id = task.id
        first_id = first.id
    orchestrator = make_orchestrator(sessions, tmp_path, FakeRunner())

    feedback, snapshot_ids = orchestrator._selected_feedback(task_id)
    assert [item["body"] for item in feedback] == ["first"]
    with sessions() as session:
        task = session.get(Task, task_id)
        second = PRFeedback(
            task_id=task_id,
            provider_id="comment:second",
            kind="issue_comment",
            author="maintainer",
            author_association="MEMBER",
            body="second",
            url="https://github.com/octo/demo/pull/9#second",
            selected_at=task.created_at,
        )
        session.add(second)
        session.commit()
        second_id = second.id

    orchestrator._mark_feedback_processed(task_id, snapshot_ids)

    with sessions() as session:
        assert session.get(PRFeedback, first_id).processed_at is not None
        assert session.get(PRFeedback, second_id).processed_at is None


@pytest.mark.asyncio
async def test_legacy_manual_task_publishes_only_final_developer_report(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    workspace = tmp_path / "workspaces" / "task-1"
    workspace.mkdir(parents=True)
    with sessions() as session:
        task = create_task(session)
        task.status = "queued"
        task.failure_code = "publish_existing"
        task.workspace_path = str(workspace)
        task.branch_name = "coderus/issue-1-1"
        task.base_commit_sha = "a" * 40
        add_developer_report(session, task.id)
        session.add_all(
            [
                Review(
                    task_id=task.id, reviewer_role="reviewer_a",
                    decision="changes_requested",
                    findings=[{"severity": "high", "message": "old resolved finding"}],
                    blocking_count=1,
                ),
                Review(
                    task_id=task.id, reviewer_role="reviewer_a", decision="approve",
                    findings=[], blocking_count=0,
                ),
                Review(
                    task_id=task.id, reviewer_role="reviewer_b",
                    decision="changes_requested",
                    findings=[{"severity": "medium", "message": "credential edge case remains"}],
                    blocking_count=1,
                ),
            ]
        )
        session.commit()
        task_id = task.id
    runner = FakeRunner()
    publisher = FakePublisher()
    orchestrator = make_orchestrator(sessions, tmp_path, runner, publisher)

    await orchestrator.run(task_id)

    assert runner.specs == []
    assert "credential edge case remains" not in publisher.calls[0]["body"]
    assert "old resolved finding" not in publisher.calls[0]["body"]
    with sessions() as session:
        assert session.get(Task, task_id).status == "awaiting_human_review"


@pytest.mark.asyncio
async def test_publish_retry_reuses_existing_commit_without_new_changes(
    engine, tmp_path: Path
) -> None:
    sessions = create_session_factory(engine)
    workspace = tmp_path / "workspaces" / "task-1"
    workspace.mkdir(parents=True)
    with sessions() as session:
        task = create_task(session)
        task.status = "queued"
        task.failure_code = "publish_existing"
        task.workspace_path = str(workspace)
        task.branch_name = "coderus/issue-1-1"
        task.base_commit_sha = "a" * 40
        task.commit_sha = "c" * 40
        add_developer_report(session, task.id)
        session.commit()
        task_id = task.id
    publisher = FakePublisher()
    orchestrator = TaskOrchestrator(
        session_factory=sessions,
        runner=FakeRunner(),
        workspace_git=FakeGit(workspace, has_changes=False),
        forges=ForgeRegistry({"github": publisher}),
        artifacts_root=tmp_path / "artifacts",
        git_user_name="bot",
        git_user_email="bot@example.com",
    )

    await orchestrator.run(task_id)

    assert len(publisher.calls) == 1
    with sessions() as session:
        assert session.get(Task, task_id).status == "awaiting_human_review"
