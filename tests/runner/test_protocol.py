from pathlib import Path

import pytest

from coderus.runner import AgentRole, JobSpec, Stage


def test_pr_reviewer_is_read_only() -> None:
    assert AgentRole.PR_REVIEWER.read_only is True


def test_pr_review_requires_a_base_commit(tmp_path: Path) -> None:
    spec = JobSpec(
        job_id="review-1",
        stage=Stage.PR_REVIEW,
        role=AgentRole.PR_REVIEWER,
        workspace=tmp_path,
        prompt="Format the review",
        review_base="a" * 40,
    )

    assert spec.review_base == "a" * 40


@pytest.mark.parametrize("review_base", [None, "main", "a" * 39, "-" * 40])
def test_pr_review_rejects_a_missing_or_invalid_base_commit(
    tmp_path: Path, review_base: str | None
) -> None:
    with pytest.raises(ValueError, match="review_base"):
        JobSpec(
            job_id="review-1",
            stage=Stage.PR_REVIEW,
            role=AgentRole.PR_REVIEWER,
            workspace=tmp_path,
            prompt="Format the review",
            review_base=review_base,
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
