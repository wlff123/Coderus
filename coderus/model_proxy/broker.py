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
    max_concurrency: int
    in_flight: int = 0


@dataclass
class _Usage:
    max_requests: int
    max_output_bytes: int
    request_count: int = 0
    output_bytes: int = 0


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

    def record_output(self, size: int) -> None:
        self._broker._record_output(self._digest, size)


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
        self._usage_by_task_stage: dict[tuple[str, str], _Usage] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        task_id: str,
        stage: str,
        ttl_seconds: float | None = None,
        max_requests: int = 256,
        max_concurrency: int = 1,
        max_output_bytes: int = 10 * 1024 * 1024,
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
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be a positive integer")

        token = secrets.token_urlsafe(32)
        with self._lock:
            usage_key = (task_id, stage)
            usage = self._usage_by_task_stage.get(usage_key)
            if usage is None:
                self._usage_by_task_stage[usage_key] = _Usage(
                    max_requests=max_requests,
                    max_output_bytes=max_output_bytes,
                )
            else:
                usage.max_requests = min(usage.max_requests, max_requests)
                usage.max_output_bytes = min(usage.max_output_bytes, max_output_bytes)
            self._leases_by_digest[self._digest(token)] = _Lease(
                expires_at=self._clock() + ttl,
                task_id=task_id,
                stage=stage,
                model=self._configured_model,
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
                self._remove_lease(digest)
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
                self._remove_lease(digest)
                raise LeaseRejected(
                    "Invalid or expired bearer token", status_code=401
                )
            if endpoint != _RESPONSES_ENDPOINT:
                raise LeaseRejected("Lease does not permit endpoint", status_code=403)
            if requested_model != lease.model:
                raise LeaseRejected("Lease does not permit model", status_code=403)
            usage = self._usage_by_task_stage[(lease.task_id, lease.stage)]
            if usage.request_count >= usage.max_requests:
                raise LeaseRejected("Lease request limit reached", status_code=429)
            if lease.in_flight >= lease.max_concurrency:
                raise LeaseRejected("Lease concurrency limit reached", status_code=429)
            usage.request_count += 1
            lease.in_flight += 1
            return LeasePermit(lease.task_id, lease.stage, self, digest)

    def revoke(self, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            return self._remove_lease(self._digest(token))

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

    def _record_output(self, digest: bytes, size: int) -> None:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("output size must be a non-negative integer")
        with self._lock:
            lease = self._leases_by_digest.get(digest)
            if lease is None:
                raise LeaseRejected("Invalid or expired bearer token", status_code=401)
            usage = self._usage_by_task_stage[(lease.task_id, lease.stage)]
            if usage.output_bytes + size > usage.max_output_bytes:
                raise LeaseRejected("Lease output limit reached", status_code=429)
            usage.output_bytes += size

    def _remove_lease(self, digest: bytes) -> bool:
        lease = self._leases_by_digest.pop(digest, None)
        if lease is None:
            return False
        usage_key = (lease.task_id, lease.stage)
        if not any(
            (other.task_id, other.stage) == usage_key
            for other in self._leases_by_digest.values()
        ):
            self._usage_by_task_stage.pop(usage_key, None)
        return True

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()
