from pathlib import Path

import pytest

from coderus.runner import AgentRole, JobSpec, Stage


def test_pr_reviewer_is_read_only() -> None:
    assert AgentRole.PR_REVIEWER.read_only is True


def test_pr_review_accepts_a_structured_output_schema(tmp_path: Path) -> None:
    schema = tmp_path / "review.schema.json"
    spec = JobSpec(
        job_id="review-1",
        stage=Stage.PR_REVIEW,
        role=AgentRole.PR_REVIEWER,
        workspace=tmp_path,
        prompt="Review the diff",
        output_schema=schema,
        review_base="coderus-review-base",
    )

    assert spec.output_schema == schema


def test_pr_review_requires_a_review_base(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="review_base"):
        JobSpec(
            job_id="review-1",
            stage=Stage.PR_REVIEW,
            role=AgentRole.PR_REVIEWER,
            workspace=tmp_path,
            prompt="Review the diff",
        )


def test_pr_review_accepts_a_review_base(tmp_path: Path) -> None:
    spec = JobSpec(
        job_id="pr-review-1",
        stage=Stage.PR_REVIEW,
        role=AgentRole.PR_REVIEWER,
        workspace=tmp_path,
        prompt="Use Chinese for structured review feedback.",
        review_base="coderus-review-base",
    )

    assert spec.review_base == "coderus-review-base"


@pytest.mark.parametrize(
    ("stage", "role"),
    [
        (Stage.DEVELOP, AgentRole.DEVELOPER),
        (Stage.REVIEW_CORRECTNESS, AgentRole.REVIEWER_A),
        (Stage.REVIEW_SECURITY, AgentRole.REVIEWER_B),
        (Stage.REVISE, AgentRole.DEVELOPER),
    ],
)
def test_non_pr_review_stages_reject_a_review_base(
    tmp_path: Path, stage: Stage, role: AgentRole
) -> None:
    with pytest.raises(ValueError, match="review_base"):
        JobSpec(
            job_id="run-1",
            stage=stage,
            role=role,
            workspace=tmp_path,
            prompt="Run the stage",
            review_base="coderus-review-base",
        )


def test_pr_review_rejects_a_session_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session_id"):
        JobSpec(
            job_id="review-1",
            stage=Stage.PR_REVIEW,
            role=AgentRole.PR_REVIEWER,
            workspace=tmp_path,
            prompt="Review the diff",
            review_base="coderus-review-base",
            session_id="session-1",
        )


def test_job_spec_accepts_the_role_assigned_to_a_stage(tmp_path: Path) -> None:
    spec = JobSpec(
        job_id="run-1",
        stage=Stage.DEVELOP,
        role=AgentRole.DEVELOPER,
        workspace=tmp_path,
        prompt="Implement the issue",
    )

    assert spec.stage is Stage.DEVELOP
    assert spec.role is AgentRole.DEVELOPER


def test_job_spec_rejects_a_role_not_assigned_to_the_stage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match stage"):
        JobSpec(
            job_id="run-1",
            stage=Stage.REVIEW_CORRECTNESS,
            role=AgentRole.DEVELOPER,
            workspace=tmp_path,
            prompt="Review the issue",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("timeout_seconds", 0), ("max_output_bytes", 0)],
)
def test_job_spec_rejects_non_positive_limits(tmp_path: Path, field: str, value: int) -> None:
    values = {
        "job_id": "run-1",
        "stage": Stage.DEVELOP,
        "role": AgentRole.DEVELOPER,
        "workspace": tmp_path,
        "prompt": "Develop the issue",
        field: value,
    }

    with pytest.raises(ValueError, match=field):
        JobSpec(**values)
