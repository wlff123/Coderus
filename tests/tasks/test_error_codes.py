from __future__ import annotations

import pytest

from coderus.forge.errors import (
    GitPushError,
    InvalidProviderUrl,
    ProviderRemoteError,
    PublisherRemoteError,
)
from coderus.forge.registry import ForgeNotConfigured
from coderus.pr_review.orchestrator import PRReviewError
from coderus.pr_review.result import ReviewOutputError
from coderus.processes import CommandOutputLimitExceeded, CommandResourceLimitExceeded
from coderus.tasks.error_codes import TaskErrorCode, classify_exception


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ForgeNotConfigured("github"), TaskErrorCode.FORGE_AUTH_FAILED),
        (
            ProviderRemoteError("github", "forbidden", status_code=403),
            TaskErrorCode.FORGE_AUTH_FAILED,
        ),
        (
            ProviderRemoteError("github", "rate limited", status_code=429),
            TaskErrorCode.UPSTREAM_UNAVAILABLE,
        ),
        (PublisherRemoteError("server error"), TaskErrorCode.UPSTREAM_UNAVAILABLE),
        (GitPushError("push rejected"), TaskErrorCode.UPSTREAM_UNAVAILABLE),
        (InvalidProviderUrl("bad url"), TaskErrorCode.INVALID_INPUT),
        (PRReviewError("PR 已合并"), TaskErrorCode.INVALID_INPUT),
        (ReviewOutputError("bad output"), TaskErrorCode.AGENT_OUTPUT_INVALID),
        (CommandResourceLimitExceeded("too big"), TaskErrorCode.RESOURCE_LIMIT_EXCEEDED),
        (CommandOutputLimitExceeded("too much"), TaskErrorCode.RESOURCE_LIMIT_EXCEEDED),
        (TimeoutError(), TaskErrorCode.SIDE_EFFECT_UNKNOWN),
        (ConnectionError("reset"), TaskErrorCode.UPSTREAM_UNAVAILABLE),
        (ValueError("bad value"), TaskErrorCode.INVALID_INPUT),
        (RuntimeError("boom"), TaskErrorCode.INTERNAL_ERROR),
    ],
)
def test_classify_exception(exc: BaseException, expected: TaskErrorCode) -> None:
    assert classify_exception(exc) is expected


def test_codes_are_stable_snake_case_strings() -> None:
    assert {code.value for code in TaskErrorCode} == {
        "invalid_input",
        "forge_auth_failed",
        "upstream_unavailable",
        "repository_build_failed",
        "agent_output_invalid",
        "tests_failed",
        "resource_limit_exceeded",
        "executor_interrupted",
        "side_effect_unknown",
        "internal_error",
    }
