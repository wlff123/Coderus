from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

_RESPONSES_ENDPOINT = "/v1/responses"


class LeaseRejected(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class _Lease:
    expires_at: float
    task_id: str
    stage: str
    model: str
    max_requests: int
    max_concurrency: int
    request_count: int = 0
    in_flight: int = 0


@dataclass
class LeasePermit:
    task_id: str
    stage: str
    _broker: CredentialBroker = field(repr=False)
    _digest: bytes = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)
    _release_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._broker._release(self._digest)


class CredentialBroker:
    """Issue and validate opaque, short-lived bearer tokens in memory."""

    def __init__(
        self,
        *,
        configured_model: str,
        default_ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(configured_model, str) or not configured_model.strip():
            raise ValueError("configured_model must be non-empty")
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        self._configured_model = configured_model
        self._default_ttl_seconds = default_ttl_seconds
        self._clock = clock
        self._leases_by_digest: dict[bytes, _Lease] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        task_id: str,
        stage: str,
        ttl_seconds: float | None = None,
        max_requests: int = 256,
        max_concurrency: int = 1,
    ) -> str:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be non-empty")
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("stage must be non-empty")
        ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests <= 0:
            raise ValueError("max_requests must be a positive integer")
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")

        token = secrets.token_urlsafe(32)
        with self._lock:
            self._leases_by_digest[self._digest(token)] = _Lease(
                expires_at=self._clock() + ttl,
                task_id=task_id,
                stage=stage,
                model=self._configured_model,
                max_requests=max_requests,
                max_concurrency=max_concurrency,
            )
        return token

    def validate(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False

        digest = self._digest(token)
        with self._lock:
            lease = self._leases_by_digest.get(digest)
            if lease is None:
                return False
            if self._clock() >= lease.expires_at:
                del self._leases_by_digest[digest]
                return False
            return True

    def acquire(
        self, token: str, *, endpoint: str, requested_model: str
    ) -> LeasePermit:
        if not isinstance(token, str) or not token:
            raise LeaseRejected(
                "Invalid or expired bearer token", status_code=401
            )
        digest = self._digest(token)
        with self._lock:
            lease = self._leases_by_digest.get(digest)
            if lease is None or self._clock() >= lease.expires_at:
                self._leases_by_digest.pop(digest, None)
                raise LeaseRejected(
                    "Invalid or expired bearer token", status_code=401
                )
            if endpoint != _RESPONSES_ENDPOINT:
                raise LeaseRejected("Lease does not permit endpoint", status_code=403)
            if requested_model != lease.model:
                raise LeaseRejected("Lease does not permit model", status_code=403)
            if lease.request_count >= lease.max_requests:
                raise LeaseRejected("Lease request limit reached", status_code=429)
            if lease.in_flight >= lease.max_concurrency:
                raise LeaseRejected("Lease concurrency limit reached", status_code=429)
            lease.request_count += 1
            lease.in_flight += 1
            return LeasePermit(lease.task_id, lease.stage, self, digest)

    def revoke(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            return self._leases_by_digest.pop(self._digest(token), None) is not None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(default_ttl_seconds={self._default_ttl_seconds!r}, "
            f"stored_digests={len(self._leases_by_digest)})"
        )

    def _release(self, digest: bytes) -> None:
        with self._lock:
            lease = self._leases_by_digest.get(digest)
            if lease is not None and lease.in_flight > 0:
                lease.in_flight -= 1

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()
