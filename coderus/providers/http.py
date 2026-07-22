import math
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import ProviderRemoteError


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, object] | None = None,
    ) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 5.0
    max_elapsed: float = 15.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay < 0 or self.max_delay < 0 or self.max_elapsed <= 0:
            raise ValueError("retry durations are invalid")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")


DEFAULT_RETRY_POLICY = RetryPolicy()


def request_with_backoff[ResponseT: HttpResponse](
    provider: str,
    request: Callable[[], ResponseT],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    random_value: Callable[[], float] = random.random,
) -> ResponseT:
    deadline = clock() + policy.max_elapsed
    for attempt in range(policy.max_attempts):
        if attempt and clock() >= deadline:
            raise ProviderRemoteError(provider, f"{provider} retry deadline exceeded")
        try:
            response = request()
        except Exception:
            if attempt + 1 == policy.max_attempts:
                raise ProviderRemoteError(provider, f"{provider} request failed") from None
        else:
            if not _retryable_status(response.status_code):
                return response
            if attempt + 1 == policy.max_attempts:
                return response

        remaining = deadline - clock()
        if remaining <= 0:
            raise ProviderRemoteError(provider, f"{provider} retry deadline exceeded")
        delay = _retry_after(response) if "response" in locals() else None
        if delay is None:
            exponential = policy.base_delay * (2**attempt)
            jitter = 1 + policy.jitter_ratio * (2 * random_value() - 1)
            delay = exponential * jitter
        sleep(min(max(0.0, delay), policy.max_delay, remaining))
        if "response" in locals():
            del response
    raise AssertionError("unreachable")


def _retryable_status(status_code: int) -> bool:
    return status_code in {408, 425, 429} or status_code >= 500


def _retry_after(response: HttpResponse) -> float | None:
    value = next(
        (value for key, value in response.headers.items() if key.casefold() == "retry-after"),
        None,
    )
    if value is None:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def default_http_client() -> HttpClient:
    import httpx

    return httpx.Client(timeout=10.0, follow_redirects=False)


def get_json(
    client: HttpClient,
    provider: str,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, object] | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    payload, _ = get_json_response(
        client,
        provider,
        url,
        headers=headers,
        params=params,
        retry_policy=retry_policy,
        sleep=sleep,
    )
    return payload


def get_json_response(
    client: HttpClient,
    provider: str,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, object] | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    random_value: Callable[[], float] = random.random,
) -> tuple[Any, Mapping[str, str]]:
    response = request_with_backoff(
        provider,
        lambda: client.get(url, headers=headers, params=params),
        policy=retry_policy,
        sleep=sleep,
        clock=clock,
        random_value=random_value,
    )

    if not 200 <= response.status_code < 300:
        retry_after = next(
            (value for key, value in response.headers.items() if key.lower() == "retry-after"),
            None,
        )
        raise ProviderRemoteError(
            provider,
            f"{provider} request failed with status {response.status_code}",
            status_code=response.status_code,
            retry_after=retry_after,
        )

    try:
        return response.json(), response.headers
    except Exception as exc:
        raise ProviderRemoteError(provider, f"{provider} returned an invalid response") from exc
