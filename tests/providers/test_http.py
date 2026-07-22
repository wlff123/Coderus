from collections import deque

import pytest

from coderus.providers.errors import ProviderRemoteError
from coderus.providers.http import RetryPolicy, request_with_backoff


class Response:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


def test_idempotent_request_honors_retry_after_then_succeeds() -> None:
    responses = deque([Response(429, {"Retry-After": "2.5"}), Response(200)])
    sleeps: list[float] = []

    response = request_with_backoff(
        "github",
        lambda: responses.popleft(),
        policy=RetryPolicy(max_attempts=3, base_delay=1, max_delay=10, max_elapsed=20),
        sleep=sleeps.append,
        clock=lambda: 0,
        random_value=lambda: 0.5,
    )

    assert response.status_code == 200
    assert sleeps == [2.5]


def test_idempotent_request_uses_bounded_exponential_backoff_with_jitter() -> None:
    responses = deque([OSError("network"), Response(500), Response(200)])
    sleeps: list[float] = []

    response = request_with_backoff(
        "gitcode",
        lambda: (_ for _ in ()).throw(responses.popleft())
        if isinstance(responses[0], Exception)
        else responses.popleft(),
        policy=RetryPolicy(
            max_attempts=3,
            base_delay=1,
            max_delay=2,
            max_elapsed=10,
            jitter_ratio=0.2,
        ),
        sleep=sleeps.append,
        clock=lambda: 0,
        random_value=lambda: 1,
    )

    assert response.status_code == 200
    assert sleeps == [1.2, 2]


def test_idempotent_request_stops_at_total_deadline() -> None:
    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.now += delay

    clock = Clock()

    with pytest.raises(ProviderRemoteError, match="retry deadline"):
        request_with_backoff(
            "github",
            lambda: Response(500),
            policy=RetryPolicy(
                max_attempts=10,
                base_delay=2,
                max_delay=2,
                max_elapsed=3,
                jitter_ratio=0,
            ),
            sleep=clock.sleep,
            clock=clock,
            random_value=lambda: 0.5,
        )

    assert clock.now == 3
