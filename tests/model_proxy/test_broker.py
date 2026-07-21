from __future__ import annotations

import re

import pytest

from coderus.model_proxy import CredentialBroker, LeaseRejected


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_issue_creates_distinct_random_tokens_that_validate() -> None:
    broker = CredentialBroker(configured_model="test-model")

    first = broker.issue(task_id="task-1", stage="develop")
    second = broker.issue(task_id="task-2", stage="pr_review")

    assert first != second
    assert len(first) >= 32
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)
    assert broker.validate(first)
    assert broker.validate(second)
    assert not broker.validate("not-issued")


def test_token_expires_at_its_ttl() -> None:
    clock = FakeClock()
    broker = CredentialBroker(
        configured_model="test-model", default_ttl_seconds=30, clock=clock
    )
    token = broker.issue(task_id="task-1", stage="develop")

    clock.now = 129.999
    assert broker.validate(token)

    clock.now = 130.0
    assert not broker.validate(token)


def test_revoke_invalidates_only_the_selected_token() -> None:
    broker = CredentialBroker(configured_model="test-model")
    revoked = broker.issue(task_id="task-1", stage="develop")
    retained = broker.issue(task_id="task-2", stage="develop")

    assert broker.revoke(revoked)
    assert not broker.revoke(revoked)
    assert not broker.validate(revoked)
    assert broker.validate(retained)


def test_issue_accepts_a_per_token_ttl() -> None:
    clock = FakeClock()
    broker = CredentialBroker(
        configured_model="test-model", default_ttl_seconds=30, clock=clock
    )
    token = broker.issue(task_id="task-1", stage="develop", ttl_seconds=2)

    clock.now = 102.0
    assert not broker.validate(token)


@pytest.mark.parametrize("ttl", [0, -1])
def test_non_positive_ttl_is_rejected(ttl: float) -> None:
    broker = CredentialBroker(configured_model="test-model")

    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        broker.issue(task_id="task-1", stage="develop", ttl_seconds=ttl)


def test_repr_does_not_reveal_issued_tokens() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-1", stage="develop")

    rendered = repr(broker)

    assert token not in rendered
    assert "CredentialBroker" in rendered


def test_lease_is_bound_to_endpoint_and_configured_model() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(task_id="task-17", stage="pr_review")

    permit = broker.acquire(
        token, endpoint="/v1/responses", requested_model="test-model"
    )

    assert permit.task_id == "task-17"
    assert permit.stage == "pr_review"
    permit.release()
    with pytest.raises(LeaseRejected) as endpoint_error:
        broker.acquire(token, endpoint="/v1/chat/completions", requested_model="test-model")
    assert endpoint_error.value.status_code == 403
    with pytest.raises(LeaseRejected) as model_error:
        broker.acquire(token, endpoint="/v1/responses", requested_model="other-model")
    assert model_error.value.status_code == 403


def test_lease_caps_request_count_and_concurrency() -> None:
    broker = CredentialBroker(configured_model="test-model")
    token = broker.issue(
        task_id="task-1", stage="develop", max_requests=2, max_concurrency=1
    )

    first = broker.acquire(
        token, endpoint="/v1/responses", requested_model="test-model"
    )
    with pytest.raises(LeaseRejected) as concurrent_error:
        broker.acquire(token, endpoint="/v1/responses", requested_model="test-model")
    assert concurrent_error.value.status_code == 429
    first.release()
    second = broker.acquire(
        token, endpoint="/v1/responses", requested_model="test-model"
    )
    second.release()
    with pytest.raises(LeaseRejected) as exhausted_error:
        broker.acquire(token, endpoint="/v1/responses", requested_model="test-model")
    assert exhausted_error.value.status_code == 429
