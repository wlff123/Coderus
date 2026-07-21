from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from coderus.providers.errors import InvalidProviderUrl
from coderus.providers.urls import parse_pull_request_url

from .errors import (
    ForkNotReady,
    InvalidPublisherInput,
    PublisherRemoteError,
    RegisteredForkMismatch,
    UnsupportedPublisher,
)
from .git_transport import HttpsGitPusher
from .models import (
    ForkResult,
    PRCommentResult,
    PRFeedbackItem,
    PublishResult,
    PullRequestDetails,
)

_API_ROOT = "https://api.gitcode.com/api/v5"
_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_INVALID_REF_CHARACTERS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SHA_PATTERN = re.compile(r"[0-9A-Fa-f]{40}\Z")
_COMMENT_FRAGMENT = re.compile(r"[A-Za-z0-9_.-]+\Z")


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

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
    ) -> HttpResponse: ...


class GitPusher(Protocol):
    def push(self, workspace: Path, remote_url: str, branch: str) -> None: ...


class _GitCodeRequestError(PublisherRemoteError):
    def __init__(self, message: str, *, uncertain: bool) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def default_http_client() -> HttpClient:
    import httpx

    return httpx.Client(timeout=10.0, follow_redirects=False)


class GitCodePublisher:
    def __init__(
        self,
        token: str,
        account_name: str,
        *,
        registered_forks: Mapping[tuple[str, str], str],
        http_client: HttpClient | None = None,
        git_pusher: GitPusher | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fork_poll_attempts: int = 10,
        fork_poll_interval: float = 1.0,
        request_attempts: int = 3,
        retry_interval: float = 1.0,
        max_retry_delay: float = 30.0,
        max_retry_elapsed_seconds: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(token, str) or not token:
            raise InvalidPublisherInput("gitcode token must not be empty")
        self._validate_name(account_name, "gitcode account")
        if fork_poll_attempts < 1 or request_attempts < 1:
            raise ValueError("attempt counts must be positive")
        if fork_poll_interval < 0 or retry_interval < 0 or max_retry_delay < 0:
            raise ValueError("retry intervals must not be negative")
        if max_retry_elapsed_seconds <= 0:
            raise ValueError("max_retry_elapsed_seconds must be positive")
        self._token = token
        self._account_name = account_name
        self._registered_forks = {
            (owner.casefold(), name.casefold()): self._normalize_gitcode_url(url)
            for (owner, name), url in registered_forks.items()
        }
        self._http_client = http_client if http_client is not None else default_http_client()
        self._git_pusher = git_pusher or HttpsGitPusher(token, account_name)
        self._sleep = sleep
        self._fork_poll_attempts = fork_poll_attempts
        self._fork_poll_interval = fork_poll_interval
        self._request_attempts = request_attempts
        self._retry_interval = retry_interval
        self._max_retry_delay = max_retry_delay
        self._max_retry_elapsed_seconds = max_retry_elapsed_seconds
        self._clock = clock
        self._permission_cache: dict[tuple[str, str, str], str] = {}

    def ensure_fork(self, upstream_owner: str, repository_name: str) -> ForkResult:
        self._validate_name(upstream_owner, "upstream owner")
        self._validate_name(repository_name, "repository name")
        identity = self._get_json(f"{_API_ROOT}/user")
        login = identity.get("login")
        if not isinstance(login, str) or _NAME_PATTERN.fullmatch(login) is None:
            raise PublisherRemoteError("gitcode returned an invalid response")
        if login.casefold() != self._account_name.casefold():
            raise RegisteredForkMismatch("gitcode account does not match configured account")

        fork_api_url = f"{_API_ROOT}/repos/{login}/{repository_name}"
        payload = self._get_optional_json(fork_api_url)
        created = False
        if payload is None:
            created = True
            try:
                self._post_json(
                    f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}/forks",
                    {"name": repository_name, "path": repository_name},
                )
            except _GitCodeRequestError as error:
                if not error.uncertain:
                    raise
            for _ in range(self._fork_poll_attempts):
                self._sleep(self._fork_poll_interval)
                payload = self._get_optional_json(fork_api_url)
                if payload is not None:
                    break
            else:
                raise ForkNotReady("gitcode fork did not become available")

        return self._fork_result(
            payload,
            login=login,
            upstream_owner=upstream_owner,
            repository_name=repository_name,
            created=created,
        )

    def publish(
        self,
        workspace: Path,
        upstream_owner: str,
        repository_name: str,
        default_branch: str,
        branch: str,
        title: str,
        body: str,
    ) -> PublishResult:
        self._validate_name(upstream_owner, "upstream owner")
        self._validate_name(repository_name, "repository name")
        self._validate_branch(default_branch)
        self._validate_branch(branch)
        if not isinstance(title, str) or not title.strip():
            raise InvalidPublisherInput("pull request title must not be empty")
        if not isinstance(body, str):
            raise InvalidPublisherInput("pull request body must be a string")
        workspace = Path(workspace)
        if not workspace.is_dir():
            raise InvalidPublisherInput("publish workspace must be an existing directory")

        key = (upstream_owner.casefold(), repository_name.casefold())
        registered_fork_url = self._registered_forks.get(key)
        if registered_fork_url is None:
            raise RegisteredForkMismatch("no fork is registered for the upstream repository")
        fork = self.ensure_fork(upstream_owner, repository_name)
        if fork.url != registered_fork_url:
            raise RegisteredForkMismatch("gitcode fork does not match the registered fork URL")

        self._git_pusher.push(workspace, registered_fork_url, branch)
        existing = self._find_pull_request(
            upstream_owner,
            repository_name,
            fork.owner,
            branch,
            default_branch,
        )
        if existing is not None:
            return self._publish_result(
                existing,
                registered_fork_url,
                branch,
                pr_created=False,
            )

        pulls_url = f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}/pulls"
        try:
            payload = self._post_json(
                pulls_url,
                {
                    "title": title.strip(),
                    "head": f"{fork.owner}:{branch}",
                    "fork_path": f"{fork.owner}/{repository_name}",
                    "base": default_branch,
                    "body": body,
                    "draft": False,
                },
            )
        except _GitCodeRequestError as error:
            if not error.uncertain:
                raise
            reconciled = self._find_pull_request(
                upstream_owner,
                repository_name,
                fork.owner,
                branch,
                default_branch,
            )
            if reconciled is None:
                raise error from None
            payload = reconciled
        return self._publish_result(
            payload,
            registered_fork_url,
            branch,
            pr_created=True,
        )

    def list_pr_feedback(
        self, upstream_owner: str, repository_name: str, pr_number: int
    ) -> list[PRFeedbackItem]:
        self._validate_coordinates(upstream_owner, repository_name, pr_number)
        url = (
            f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}"
            f"/pulls/{pr_number}/comments"
        )
        feedback: list[PRFeedbackItem] = []
        for payload in self._get_list_pages(url, params={}):
            feedback.append(
                self._feedback_item(
                    upstream_owner, repository_name, pr_number, payload
                )
            )
        return feedback

    def get_pr_status(
        self, upstream_owner: str, repository_name: str, pr_number: int
    ) -> str:
        self._validate_coordinates(upstream_owner, repository_name, pr_number)
        payload = self._get_json(
            f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}/pulls/{pr_number}"
        )
        state, merged = self._pr_state(payload)
        return "merged" if merged else state

    def get_pull_request(
        self, upstream_owner: str, repository_name: str, pr_number: int
    ) -> PullRequestDetails:
        self._validate_coordinates(upstream_owner, repository_name, pr_number)
        payload = self._get_json(
            f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}/pulls/{pr_number}"
        )
        return self._pull_request_details(
            payload, upstream_owner, repository_name, pr_number
        )

    def publish_pr_comment(
        self,
        upstream_owner: str,
        repository_name: str,
        pr_number: int,
        body: str,
        marker: str,
        *,
        path: str | None = None,
        position: int | None = None,
    ) -> PRCommentResult:
        self._validate_coordinates(upstream_owner, repository_name, pr_number)
        if not isinstance(marker, str) or not marker:
            raise InvalidPublisherInput("pull request comment marker must not be empty")
        if not isinstance(body, str) or not body.endswith(marker):
            raise InvalidPublisherInput("pull request comment body must end with its marker")
        if (path is None) != (position is None):
            raise InvalidPublisherInput("diff comments require both path and position")
        if path is not None and (not isinstance(path, str) or not path):
            raise InvalidPublisherInput("diff comment path must not be empty")
        if position is not None and (
            isinstance(position, bool) or not isinstance(position, int) or position < 1
        ):
            raise InvalidPublisherInput("diff comment position must be positive")

        comments_url = (
            f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}"
            f"/pulls/{pr_number}/comments"
        )
        existing = self._find_comment(comments_url, marker)
        if existing is not None:
            return PRCommentResult(
                url=self._comment_url(
                    existing.get("html_url"),
                    upstream_owner,
                    repository_name,
                    pr_number,
                ),
                created=False,
            )

        request_body: dict[str, object] = {"body": body}
        if path is not None and position is not None:
            request_body.update({"path": path, "position": position})
        try:
            result = self._post_json(comments_url, request_body)
            self._validate_created_comment(result, body)
        except _GitCodeRequestError as error:
            if not error.uncertain:
                raise
            reconciled = self._find_comment(comments_url, marker)
            if reconciled is None:
                raise error from None
            return PRCommentResult(
                url=self._comment_url(
                    reconciled.get("html_url"),
                    upstream_owner,
                    repository_name,
                    pr_number,
                ),
                created=True,
            )
        return PRCommentResult(
            url=self._comment_url(
                result.get("html_url"),
                upstream_owner,
                repository_name,
                pr_number,
            ),
            created=True,
        )

    def _validate_created_comment(
        self, result: Mapping[str, Any], expected_body: str
    ) -> None:
        try:
            self._comment_identifier(result.get("id"), "comment response")
        except PublisherRemoteError:
            raise _GitCodeRequestError(
                "gitcode returned an invalid comment response", uncertain=True
            ) from None
        if result.get("body") != expected_body:
            raise _GitCodeRequestError(
                "gitcode returned an invalid comment response", uncertain=True
            )

    def _find_comment(
        self, comments_url: str, marker: str
    ) -> dict[str, Any] | None:
        for comment in self._get_list_pages(comments_url, params={}):
            existing_body = comment.get("body")
            user = comment.get("user")
            if (
                isinstance(existing_body, str)
                and existing_body.endswith(marker)
                and isinstance(user, dict)
                and isinstance(user.get("login"), str)
                and user["login"].casefold() == self._account_name.casefold()
            ):
                self._comment_identifier(comment.get("id"), "comment response")
                return comment
        return None

    def _find_pull_request(
        self,
        upstream_owner: str,
        repository_name: str,
        fork_owner: str,
        branch: str,
        default_branch: str,
    ) -> dict[str, Any] | None:
        url = f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}/pulls"
        for payload in self._get_list_pages(
            url,
            params={
                "state": "all",
                "base": default_branch,
            },
        ):
            head = payload.get("head")
            base = payload.get("base")
            if not isinstance(head, dict) or not isinstance(base, dict):
                raise PublisherRemoteError("gitcode returned an invalid response")
            head_repo = head.get("repo")
            base_repo = base.get("repo")
            if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
                raise PublisherRemoteError("gitcode returned an invalid response")
            head_coordinates = self._repository_coordinates(head_repo)
            base_coordinates = self._repository_coordinates(base_repo)
            if (
                head.get("ref") == branch
                and tuple(part.casefold() for part in head_coordinates)
                == (fork_owner.casefold(), repository_name.casefold())
                and base.get("ref") == default_branch
                and tuple(part.casefold() for part in base_coordinates)
                == (upstream_owner.casefold(), repository_name.casefold())
            ):
                return payload
        return None

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._request("GET", url)
        payload = self._response_json(response)
        if not isinstance(payload, dict):
            raise PublisherRemoteError("gitcode returned an invalid response")
        return payload

    def _get_optional_json(self, url: str) -> dict[str, Any] | None:
        response = self._request("GET", url, allowed_statuses={404})
        if response.status_code == 404:
            return None
        payload = self._response_json(response)
        if not isinstance(payload, dict):
            raise PublisherRemoteError("gitcode returned an invalid response")
        return payload

    def _post_json(self, url: str, payload: Mapping[str, object]) -> dict[str, Any]:
        response = self._request("POST", url, payload=payload)
        try:
            result = self._response_json(response)
        except PublisherRemoteError:
            raise _GitCodeRequestError(
                "gitcode returned an invalid response", uncertain=True
            ) from None
        if not isinstance(result, dict):
            raise _GitCodeRequestError(
                "gitcode returned an invalid response", uncertain=True
            )
        return result

    def _get_list_pages(
        self, url: str, *, params: Mapping[str, object]
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            page_params = {**params, "per_page": 100, "page": page}
            response = self._request("GET", url, params=page_params)
            payload = self._response_json(response)
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                raise PublisherRemoteError("gitcode returned an invalid response")
            yield from payload
            if len(payload) < 100:
                return
            page += 1

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: set[int] | None = None,
    ) -> HttpResponse:
        allowed_statuses = allowed_statuses or set()
        is_post = method == "POST"
        deadline = self._clock() + self._max_retry_elapsed_seconds
        for attempt in range(self._request_attempts):
            if attempt > 0 and self._clock() >= deadline:
                raise _GitCodeRequestError(
                    "gitcode retry deadline exceeded", uncertain=True
                )
            try:
                if method == "GET":
                    response = self._http_client.get(
                        url, headers=self._headers(), params=params
                    )
                else:
                    response = self._http_client.post(
                        url, headers=self._headers(), json=payload or {}
                    )
            except Exception:
                if is_post or attempt + 1 == self._request_attempts:
                    raise _GitCodeRequestError(
                        "gitcode request failed", uncertain=True
                    ) from None
                self._sleep_for_retry(self._retry_interval, deadline)
                continue

            if 200 <= response.status_code < 300 or response.status_code in allowed_statuses:
                return response
            if is_post and response.status_code == 409:
                raise _GitCodeRequestError(
                    "gitcode request failed with status 409", uncertain=True
                )
            rate_limited = response.status_code == 429
            server_error = response.status_code >= 500
            retryable = rate_limited or server_error
            if not retryable:
                raise _GitCodeRequestError(
                    f"gitcode request failed with status {response.status_code}",
                    uncertain=False,
                )
            if server_error and is_post:
                raise _GitCodeRequestError(
                    f"gitcode request failed with status {response.status_code}",
                    uncertain=True,
                )
            if attempt + 1 == self._request_attempts:
                raise _GitCodeRequestError(
                    f"gitcode request failed with status {response.status_code}",
                    uncertain=True,
                )
            self._sleep_for_retry(self._retry_delay(response), deadline)
        raise AssertionError("unreachable")

    def _sleep_for_retry(self, requested_delay: float, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _GitCodeRequestError(
                "gitcode retry deadline exceeded", uncertain=True
            )
        self._sleep(min(requested_delay, self._max_retry_delay, remaining))

    def _retry_delay(self, response: HttpResponse) -> float:
        if response.status_code == 429:
            value = next(
                (
                    value
                    for key, value in response.headers.items()
                    if key.casefold() == "retry-after"
                ),
                None,
            )
            if value is not None:
                try:
                    delay = float(value)
                except (TypeError, ValueError):
                    pass
                else:
                    if math.isfinite(delay) and delay >= 0:
                        return delay
        return self._retry_interval

    @staticmethod
    def _response_json(response: HttpResponse) -> Any:
        try:
            return response.json()
        except Exception:
            raise PublisherRemoteError("gitcode returned an invalid response") from None

    def _feedback_item(
        self,
        upstream_owner: str,
        repository_name: str,
        pr_number: int,
        payload: Mapping[str, Any],
    ) -> PRFeedbackItem:
        identifier = self._comment_identifier(payload.get("id"), "pull request feedback")
        body = payload.get("body")
        user = payload.get("user")
        if (
            not isinstance(body, str)
            or not body
            or not isinstance(user, dict)
            or not isinstance(user.get("login"), str)
        ):
            raise PublisherRemoteError("gitcode returned invalid pull request feedback")
        author = cast(str, user["login"])
        self._validate_name(author, "feedback author")
        path = payload.get("path")
        position = payload.get("position")
        is_diff = isinstance(path, str) and isinstance(position, int) and not isinstance(
            position, bool
        )
        return PRFeedbackItem(
            provider_id=f"comment:{identifier}",
            kind="review_comment" if is_diff else "issue_comment",
            author=author,
            author_association=self._author_association(
                upstream_owner, repository_name, author
            ),
            body=body,
            url=self._comment_url(
                payload.get("html_url"),
                upstream_owner,
                repository_name,
                pr_number,
            ),
            path=path if is_diff else None,
            line=position if is_diff else None,
        )

    def _author_association(
        self, upstream_owner: str, repository_name: str, author: str
    ) -> str:
        if author.casefold() == upstream_owner.casefold():
            return "OWNER"
        key = (
            upstream_owner.casefold(),
            repository_name.casefold(),
            author.casefold(),
        )
        cached = self._permission_cache.get(key)
        if cached is not None:
            return cached
        try:
            response = self._request(
                "GET",
                f"{_API_ROOT}/repos/{upstream_owner}/{repository_name}"
                f"/collaborators/{author}/permission",
                allowed_statuses={404},
            )
            if response.status_code == 404:
                association = "NONE"
            else:
                payload = self._response_json(response)
                if not isinstance(payload, dict) or payload.get("permission") not in {
                    "admin",
                    "push",
                    "pull",
                    "none",
                }:
                    raise PublisherRemoteError(
                        "gitcode returned an invalid permission response"
                    )
                association = {
                    "admin": "MEMBER",
                    "push": "COLLABORATOR",
                    "pull": "CONTRIBUTOR",
                    "none": "NONE",
                }[cast(str, payload["permission"])]
        except PublisherRemoteError:
            association = "NONE"
        self._permission_cache[key] = association
        return association

    @staticmethod
    def _fork_result(
        payload: Mapping[str, Any],
        *,
        login: str,
        upstream_owner: str,
        repository_name: str,
        created: bool,
    ) -> ForkResult:
        owner = payload.get("owner")
        parent = payload.get("parent")
        full_name = payload.get("full_name")
        if (
            payload.get("fork") is not True
            or payload.get("private") is not False
            or payload.get("public") is not True
            or not isinstance(owner, dict)
            or not isinstance(parent, dict)
            or not isinstance(owner.get("login"), str)
            or not isinstance(parent.get("full_name"), str)
            or not isinstance(full_name, str)
            or owner["login"].casefold() != login.casefold()
            or parent["full_name"].casefold()
            != f"{upstream_owner}/{repository_name}".casefold()
            or full_name.casefold() != f"{login}/{repository_name}".casefold()
        ):
            raise RegisteredForkMismatch("gitcode repository is not the expected public fork")
        return ForkResult(
            url=f"https://gitcode.com/{login}/{repository_name}.git",
            owner=login,
            created=created,
        )

    @staticmethod
    def _publish_result(
        payload: Mapping[str, Any], fork_url: str, branch: str, *, pr_created: bool
    ) -> PublishResult:
        url = payload.get("html_url")
        if url is None:
            url = payload.get("web_url")
        number = payload.get("number")
        state, _ = GitCodePublisher._pr_state(payload)
        if (
            not isinstance(url, str)
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
        ):
            raise PublisherRemoteError("gitcode returned an invalid response")
        return PublishResult(
            url=url,
            number=number,
            state=state,
            fork_url=fork_url,
            branch=branch,
            pr_created=pr_created,
        )

    @staticmethod
    def _pull_request_details(
        payload: Mapping[str, Any], owner: str, name: str, pr_number: int
    ) -> PullRequestDetails:
        number = payload.get("number")
        url = payload.get("html_url")
        base = payload.get("base")
        head = payload.get("head")
        state, merged = GitCodePublisher._pr_state(payload)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number != pr_number
            or not isinstance(url, str)
            or not isinstance(base, dict)
            or not isinstance(head, dict)
        ):
            raise PublisherRemoteError("gitcode returned invalid pull request metadata")
        base_repo = base.get("repo")
        head_repo = head.get("repo")
        base_sha = base.get("sha")
        head_sha = head.get("sha")
        base_ref = base.get("ref")
        head_ref = head.get("ref")
        if (
            not isinstance(base_repo, dict)
            or not isinstance(base_sha, str)
            or _SHA_PATTERN.fullmatch(base_sha) is None
            or not isinstance(head_sha, str)
            or _SHA_PATTERN.fullmatch(head_sha) is None
            or not isinstance(base_ref, str)
            or not isinstance(head_ref, str)
            or not isinstance(head_repo, dict)
        ):
            raise PublisherRemoteError("gitcode returned invalid pull request metadata")
        try:
            url_repository, url_number = parse_pull_request_url(url)
            if (
                url_repository.provider != "gitcode"
                or url_repository.owner.casefold() != owner.casefold()
                or url_repository.name.casefold() != name.casefold()
                or url_number != pr_number
            ):
                raise InvalidPublisherInput("inconsistent gitcode pull request URL")
            GitCodePublisher._validate_branch(base_ref)
            GitCodePublisher._validate_branch(head_ref)
            base_owner, base_name = GitCodePublisher._repository_coordinates(base_repo)
            if (base_owner.casefold(), base_name.casefold()) != (
                owner.casefold(),
                name.casefold(),
            ):
                raise InvalidPublisherInput("inconsistent gitcode base repository")
            head_owner, head_name = GitCodePublisher._repository_coordinates(head_repo)
            head_repository_url = f"https://gitcode.com/{head_owner}/{head_name}.git"
        except (InvalidProviderUrl, InvalidPublisherInput, UnsupportedPublisher):
            raise PublisherRemoteError("gitcode returned invalid pull request metadata") from None
        return PullRequestDetails(
            number=number,
            url=url,
            state=state,
            merged=merged,
            base_sha=base_sha.lower(),
            head_sha=head_sha.lower(),
            base_ref=base_ref,
            head_ref=head_ref,
            head_repository_url=head_repository_url,
        )

    @staticmethod
    def _repository_coordinates(repository: Mapping[str, Any]) -> tuple[str, str]:
        try:
            has_official_shape = any(
                key in repository for key in ("path", "name", "namespace")
            )
            if has_official_shape:
                path = repository.get("path")
                name = repository.get("name")
                if not isinstance(path, str) or not isinstance(name, str):
                    raise InvalidPublisherInput("invalid gitcode repository coordinates")
                if "namespace" in repository:
                    namespace = repository.get("namespace")
                    owner_value = (
                        namespace.get("path") if isinstance(namespace, dict) else None
                    )
                else:
                    full_path = repository.get("full_path")
                    if full_path is None:
                        full_path = repository.get("full_name")
                    if isinstance(full_path, str) and len(full_path.split("/")) == 2:
                        owner_value, full_path_name = full_path.split("/")
                        if full_path_name.casefold() != path.casefold():
                            raise InvalidPublisherInput(
                                "inconsistent gitcode repository path"
                            )
                    else:
                        repository_owner = repository.get("owner")
                        owner_value = (
                            repository_owner.get("login")
                            if isinstance(repository_owner, dict)
                            else None
                        )
                if not isinstance(owner_value, str):
                    raise InvalidPublisherInput("invalid gitcode repository coordinates")
                owner = owner_value
                GitCodePublisher._validate_name(owner, "repository namespace")
                GitCodePublisher._validate_name(path, "repository path")
                GitCodePublisher._validate_name(name, "repository name")
                if name.casefold() != path.casefold():
                    raise InvalidPublisherInput("inconsistent gitcode repository name")
                repository_name = path
            else:
                full_name = repository.get("full_name")
                if not isinstance(full_name, str):
                    raise InvalidPublisherInput("invalid gitcode repository coordinates")
                parts = full_name.split("/")
                if len(parts) != 2:
                    raise InvalidPublisherInput("invalid gitcode repository coordinates")
                owner, repository_name = parts
                GitCodePublisher._validate_name(owner, "repository namespace")
                GitCodePublisher._validate_name(repository_name, "repository path")

            expected_full_name = f"{owner}/{repository_name}"
            full_path = repository.get("full_path")
            if full_path is not None and (
                not isinstance(full_path, str)
                or full_path.casefold() != expected_full_name.casefold()
            ):
                raise InvalidPublisherInput("inconsistent gitcode repository coordinates")
            legacy_full_name = repository.get("full_name")
            if legacy_full_name is not None and (
                not isinstance(legacy_full_name, str)
                or legacy_full_name.casefold() != expected_full_name.casefold()
            ):
                raise InvalidPublisherInput("inconsistent gitcode repository coordinates")
            html_url = repository.get("html_url")
            if html_url is not None and (
                not isinstance(html_url, str)
                or GitCodePublisher._normalize_gitcode_url(html_url).casefold()
                != f"https://gitcode.com/{expected_full_name}.git".casefold()
            ):
                raise InvalidPublisherInput("inconsistent gitcode repository URL")
            return owner, repository_name
        except (InvalidPublisherInput, UnsupportedPublisher):
            raise PublisherRemoteError("gitcode returned an invalid response") from None

    @staticmethod
    def _pr_state(payload: Mapping[str, Any]) -> tuple[str, bool]:
        raw_state = payload.get("state")
        merged_is_present = "merged" in payload
        merged_value = payload.get("merged")
        has_merged = isinstance(merged_value, bool)
        if merged_is_present and merged_value is not None and not has_merged:
            raise PublisherRemoteError("gitcode returned an invalid pull request status")
        merged_at_is_present = "merged_at" in payload
        merged_at = payload.get("merged_at")
        if merged_at_is_present and merged_at is not None and not isinstance(
            merged_at, str
        ):
            raise PublisherRemoteError("gitcode returned an invalid pull request status")
        merged_by_date = isinstance(merged_at, str) and bool(merged_at)

        if raw_state == "merged":
            if (merged_is_present and merged_value is not True) or (
                merged_at_is_present and not merged_by_date
            ):
                raise PublisherRemoteError("gitcode returned an invalid pull request status")
            return "closed", True
        if raw_state in {"open", "opened"}:
            if merged_value is True or merged_by_date:
                raise PublisherRemoteError("gitcode returned an invalid pull request status")
            return "open", False
        if raw_state == "closed":
            if has_merged and merged_by_date and merged_value is False:
                raise PublisherRemoteError("gitcode returned an invalid pull request status")
            if has_merged:
                return "closed", cast(bool, merged_value)
            return "closed", merged_by_date
        raise PublisherRemoteError("gitcode returned an invalid pull request status")

    @staticmethod
    def _comment_identifier(value: object, label: str) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, str)) or value == "":
            raise PublisherRemoteError(f"gitcode returned an invalid {label}")
        return str(value)

    @staticmethod
    def _comment_url(value: object, owner: str, name: str, pr_number: int) -> str:
        canonical = f"https://gitcode.com/{owner}/{name}/merge_requests/{pr_number}"
        if value is None:
            return canonical
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or "%" in value
            or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise PublisherRemoteError("gitcode returned an invalid comment response")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise PublisherRemoteError("gitcode returned an invalid comment response") from None
        parts = parsed.path.split("/")[1:]
        if (
            parsed.scheme != "https"
            or parsed.hostname != "gitcode.com"
            or parsed.netloc.casefold() != "gitcode.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or len(parts) != 4
            or parts[0].casefold() != owner.casefold()
            or parts[1].casefold() != name.casefold()
            or parts[2] not in {"pulls", "merge_requests"}
            or parts[3] != str(pr_number)
            or (parsed.fragment and _COMMENT_FRAGMENT.fullmatch(parsed.fragment) is None)
        ):
            raise PublisherRemoteError("gitcode returned an invalid comment response")
        return canonical + (f"#{parsed.fragment}" if parsed.fragment else "")

    @staticmethod
    def _normalize_gitcode_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise InvalidPublisherInput("invalid gitcode fork URL") from exc
        if parsed.hostname is not None and parsed.hostname.casefold() != "gitcode.com":
            raise UnsupportedPublisher("only gitcode.com publishing is supported")
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.netloc.casefold() != "gitcode.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
            or "%" in url
            or "\\" in url
        ):
            raise InvalidPublisherInput("invalid gitcode fork URL")
        parts = parsed.path.split("/")[1:]
        if parts and parts[-1] == "":
            parts.pop()
        if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
            raise InvalidPublisherInput("invalid gitcode fork URL")
        owner, name = parts
        if name.endswith(".git"):
            name = name[:-4]
        GitCodePublisher._validate_name(owner, "fork owner")
        GitCodePublisher._validate_name(name, "fork repository name")
        return f"https://gitcode.com/{owner}/{name}.git"

    @staticmethod
    def _validate_coordinates(owner: str, name: str, pr_number: int) -> None:
        GitCodePublisher._validate_name(owner, "upstream owner")
        GitCodePublisher._validate_name(name, "repository name")
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            raise InvalidPublisherInput("pull request number must be positive")

    @staticmethod
    def _validate_name(value: str, label: str) -> None:
        if not isinstance(value, str) or _NAME_PATTERN.fullmatch(value) is None:
            raise InvalidPublisherInput(f"{label} contains unsafe characters")

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if (
            not isinstance(branch, str)
            or not branch
            or branch == "@"
            or branch.startswith(("-", ".", "/"))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "@{" in branch
            or "//" in branch
            or _INVALID_REF_CHARACTERS.search(branch) is not None
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        ):
            raise InvalidPublisherInput("invalid git branch name")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
