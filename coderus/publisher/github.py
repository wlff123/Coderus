from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlsplit

from coderus.providers.errors import ProviderRemoteError as ProviderRequestError
from coderus.providers.http import DEFAULT_RETRY_POLICY, RetryPolicy, request_with_backoff

from .errors import (
    ForkNotReady,
    InvalidPublisherInput,
    PublisherRemoteError,
    RegisteredForkMismatch,
    UnsupportedPublisher,
)
from .git_transport import GitRunner, HttpsGitPusher
from .models import (
    ForkResult,
    PRCommentResult,
    PRFeedbackItem,
    PublishResult,
    PullRequestDetails,
)

_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_INVALID_REF_CHARACTERS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SHA_PATTERN = re.compile(r"[0-9A-Fa-f]{40}\Z")


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


def default_http_client() -> HttpClient:
    import httpx

    return httpx.Client(timeout=10.0, follow_redirects=False)


class GitHubPublisher:
    _MAX_PAGES = 1000

    def __init__(
        self,
        token: str,
        *,
        registered_forks: Mapping[tuple[str, str], str],
        http_client: HttpClient | None = None,
        git_runner: GitRunner | None = None,
        git_pusher: HttpsGitPusher | None = None,
        sleep: Callable[[float], None] = time.sleep,
        fork_poll_attempts: int = 10,
        fork_poll_interval: float = 1.0,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token
        self._registered_forks = {
            (owner.lower(), name.lower()): self._normalize_github_url(url)
            for (owner, name), url in registered_forks.items()
        }
        self._http_client = http_client if http_client is not None else default_http_client()
        if git_runner is not None and git_pusher is not None:
            raise ValueError("provide git_runner or git_pusher, not both")
        self._git_pusher = git_pusher or HttpsGitPusher(
            token, "x-access-token", git_runner=git_runner
        )
        self._sleep = sleep
        self._fork_poll_attempts = fork_poll_attempts
        self._fork_poll_interval = fork_poll_interval
        self._retry_policy = retry_policy
        self._clock = clock

    def ensure_fork(self, upstream_owner: str, repository_name: str) -> ForkResult:
        self._validate_name(upstream_owner, "upstream owner")
        self._validate_name(repository_name, "repository name")
        login_payload = self._get_json("https://api.github.com/user")
        login = login_payload.get("login")
        if not isinstance(login, str) or _NAME_PATTERN.fullmatch(login) is None:
            raise PublisherRemoteError("github returned an invalid response")
        fork_url = f"https://api.github.com/repos/{login}/{repository_name}"
        payload = self._get_optional_json(fork_url)
        created = False
        if payload is None:
            response = self._post_response(
                f"https://api.github.com/repos/{upstream_owner}/{repository_name}/forks",
                {},
            )
            if not 200 <= response.status_code < 300:
                raise PublisherRemoteError(
                    f"github request failed with status {response.status_code}"
                )
            created = True
            for _ in range(self._fork_poll_attempts):
                self._sleep(self._fork_poll_interval)
                payload = self._get_optional_json(fork_url)
                if payload is not None:
                    break
            else:
                raise ForkNotReady("github fork did not become available")

        expected_parent = f"{upstream_owner}/{repository_name}"
        if (
            payload.get("fork") is not True
            or payload.get("owner", {}).get("login") != login
            or str(payload.get("parent", {}).get("full_name", "")).lower()
            != expected_parent.lower()
        ):
            raise RegisteredForkMismatch("github repository is not the expected fork")
        clone_url = payload.get("clone_url")
        if not isinstance(clone_url, str):
            raise PublisherRemoteError("github returned an invalid response")
        normalized_clone_url = self._normalize_github_url(clone_url)
        expected_clone_url = self._normalize_github_url(
            f"https://github.com/{login}/{repository_name}.git"
        )
        if normalized_clone_url.lower() != expected_clone_url.lower():
            raise RegisteredForkMismatch("github repository is not the expected fork")
        return ForkResult(
            url=normalized_clone_url,
            owner=login,
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

        key = (upstream_owner.lower(), repository_name.lower())
        registered_fork_url = self._registered_forks.get(key)
        if registered_fork_url is None:
            raise RegisteredForkMismatch("no fork is registered for the upstream repository")

        fork = self.ensure_fork(upstream_owner, repository_name)
        if fork.url != registered_fork_url:
            raise RegisteredForkMismatch("github fork does not match the registered fork URL")

        self._push(workspace, registered_fork_url, branch)
        head = f"{fork.owner}:{branch}"
        pulls_url = f"https://api.github.com/repos/{upstream_owner}/{repository_name}/pulls"
        existing = self._get_list(
            pulls_url,
            params={"state": "all", "head": head, "base": default_branch, "per_page": 100},
        )
        if existing:
            return self._publish_result(existing[0], registered_fork_url, branch, pr_created=False)

        payload = self._post_json(
            pulls_url,
            {
                "title": title.strip(),
                "head": head,
                "base": default_branch,
                "body": body,
                "draft": False,
            },
        )
        return self._publish_result(payload, registered_fork_url, branch, pr_created=True)

    def list_pr_feedback(
        self, upstream_owner: str, repository_name: str, pr_number: int
    ) -> list[PRFeedbackItem]:
        self._validate_name(upstream_owner, "upstream owner")
        self._validate_name(repository_name, "repository name")
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            raise InvalidPublisherInput("pull request number must be positive")
        base = f"https://api.github.com/repos/{upstream_owner}/{repository_name}"
        endpoints = (
            ("issue_comment", f"{base}/issues/{pr_number}/comments"),
            ("review", f"{base}/pulls/{pr_number}/reviews"),
            ("review_comment", f"{base}/pulls/{pr_number}/comments"),
        )
        feedback: list[PRFeedbackItem] = []
        for kind, url in endpoints:
            for page in self._get_list_pages(url, params={}):
                for payload in page:
                    item = self._feedback_item(kind, payload)
                    if item is not None:
                        feedback.append(item)
        return feedback

    def get_pr_status(
        self, upstream_owner: str, repository_name: str, pr_number: int
    ) -> str:
        self._validate_name(upstream_owner, "upstream owner")
        self._validate_name(repository_name, "repository name")
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            raise InvalidPublisherInput("pull request number must be positive")
        payload = self._get_json(
            f"https://api.github.com/repos/{upstream_owner}/{repository_name}/pulls/{pr_number}"
        )
        state = payload.get("state")
        merged = payload.get("merged")
        if state not in {"open", "closed"} or not isinstance(merged, bool):
            raise PublisherRemoteError("github returned an invalid pull request status")
        return "merged" if merged else state

    def get_pull_request(
        self, upstream_owner: str, repository_name: str, pr_number: int
    ) -> PullRequestDetails:
        self._validate_name(upstream_owner, "upstream owner")
        self._validate_name(repository_name, "repository name")
        self._validate_pr_number(pr_number)
        payload = self._get_json(
            f"https://api.github.com/repos/{upstream_owner}/{repository_name}/pulls/{pr_number}"
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
    ) -> PRCommentResult:
        self._validate_name(upstream_owner, "upstream owner")
        self._validate_name(repository_name, "repository name")
        self._validate_pr_number(pr_number)
        if not isinstance(marker, str) or not marker:
            raise InvalidPublisherInput("pull request comment marker must not be empty")
        if not isinstance(body, str) or not body.endswith(marker):
            raise InvalidPublisherInput("pull request comment body must end with its marker")

        login_payload = self._get_json("https://api.github.com/user")
        login = login_payload.get("login")
        if not isinstance(login, str) or _NAME_PATTERN.fullmatch(login) is None:
            raise PublisherRemoteError("github returned an invalid response")
        comments_url = (
            f"https://api.github.com/repos/{upstream_owner}/{repository_name}"
            f"/issues/{pr_number}/comments"
        )
        for comments in self._get_comment_pages(comments_url):
            for comment in comments:
                existing_body = comment.get("body")
                user = comment.get("user")
                if (
                    isinstance(existing_body, str)
                    and existing_body.endswith(marker)
                    and isinstance(user, dict)
                    and user.get("login") == login
                ):
                    url = self._comment_url(
                        comment.get("html_url"), upstream_owner, repository_name, pr_number
                    )
                    return PRCommentResult(url=url, created=False)

        result = self._post_json(comments_url, {"body": body})
        url = self._comment_url(
            result.get("html_url"), upstream_owner, repository_name, pr_number
        )
        return PRCommentResult(url=url, created=True)

    def _push(self, workspace: Path, fork_url: str, branch: str) -> None:
        self._git_pusher.push(workspace, fork_url, branch)

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._get_response(url, params=None)
        if response.status_code != 200:
            raise PublisherRemoteError(f"github request failed with status {response.status_code}")
        payload = self._response_json(response)
        if not isinstance(payload, dict):
            raise PublisherRemoteError("github returned an invalid response")
        return payload

    def _get_optional_json(self, url: str) -> dict[str, Any] | None:
        response = self._get_response(url, params=None)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PublisherRemoteError(f"github request failed with status {response.status_code}")
        payload = self._response_json(response)
        if not isinstance(payload, dict):
            raise PublisherRemoteError("github returned an invalid response")
        return payload

    def _get_list(self, url: str, *, params: Mapping[str, object]) -> list[dict[str, Any]]:
        response = self._get_response(url, params=params)
        if response.status_code != 200:
            raise PublisherRemoteError(f"github request failed with status {response.status_code}")
        payload = self._response_json(response)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise PublisherRemoteError("github returned an invalid response")
        return payload

    def _get_comment_pages(self, url: str) -> Iterator[list[dict[str, Any]]]:
        yield from self._get_list_pages(url, params={})

    def _get_list_pages(
        self, url: str, *, params: Mapping[str, object]
    ) -> Iterator[list[dict[str, Any]]]:
        page = 1
        visited: set[int] = set()
        while True:
            if page in visited or len(visited) >= self._MAX_PAGES:
                raise PublisherRemoteError("github returned invalid pagination")
            visited.add(page)
            response = self._get_response(
                url, params={**params, "per_page": 100, "page": page}
            )
            if response.status_code != 200:
                raise PublisherRemoteError(
                    f"github request failed with status {response.status_code}"
                )
            payload = self._response_json(response)
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                raise PublisherRemoteError("github returned an invalid response")
            yield payload
            next_page = self._next_page(response.headers, url, page)
            if next_page is None and len(payload) == 100:
                next_page = page + 1
            if next_page is None:
                return
            page = next_page

    @staticmethod
    def _next_page(headers: Mapping[str, str], expected_url: str, current: int) -> int | None:
        link_header = next(
            (value for key, value in headers.items() if key.casefold() == "link"), ""
        )
        next_links = [part for part in link_header.split(",") if 'rel="next"' in part]
        if not next_links:
            return None
        if len(next_links) != 1:
            raise PublisherRemoteError("github returned invalid pagination")
        start = next_links[0].find("<")
        end = next_links[0].find(">", start + 1)
        if start < 0 or end < 0:
            raise PublisherRemoteError("github returned invalid pagination")
        try:
            parsed = urlsplit(next_links[0][start + 1 : end])
            query = parse_qs(parsed.query, keep_blank_values=True)
            raw_page = query.get("page", [None])[0]
        except (TypeError, ValueError):
            raise PublisherRemoteError("github returned invalid pagination") from None
        expected = urlsplit(expected_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path != expected.path
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
            or not isinstance(raw_page, str)
            or not raw_page.isdigit()
            or int(raw_page) <= current
        ):
            raise PublisherRemoteError("github returned invalid pagination")
        return int(raw_page)

    @staticmethod
    def _feedback_item(kind: str, payload: Mapping[str, Any]) -> PRFeedbackItem | None:
        identifier = payload.get("id")
        body = payload.get("body")
        url = payload.get("html_url")
        user = payload.get("user")
        association = payload.get("author_association")
        if body in {None, ""}:
            return None
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or not isinstance(body, str)
            or not isinstance(url, str)
            or not isinstance(user, dict)
            or not isinstance(user.get("login"), str)
            or not isinstance(association, str)
            or kind not in {"issue_comment", "review", "review_comment"}
        ):
            raise PublisherRemoteError("github returned invalid pull request feedback")
        path = payload.get("path")
        line = payload.get("line")
        return PRFeedbackItem(
            provider_id=f"{kind}:{identifier}",
            kind=cast(Any, kind),
            author=user["login"],
            author_association=association,
            body=body,
            url=url,
            path=path if isinstance(path, str) else None,
            line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        )

    def _post_json(self, url: str, payload: Mapping[str, object]) -> dict[str, Any]:
        response = self._post_response(url, payload)
        if not 200 <= response.status_code < 300:
            raise PublisherRemoteError(f"github request failed with status {response.status_code}")
        result = self._response_json(response)
        if not isinstance(result, dict):
            raise PublisherRemoteError("github returned an invalid response")
        return result

    def _get_response(self, url: str, *, params: Mapping[str, object] | None) -> HttpResponse:
        try:
            return request_with_backoff(
                "github",
                lambda: self._http_client.get(url, headers=self._headers(), params=params),
                policy=self._retry_policy,
                sleep=self._sleep,
                clock=self._clock,
            )
        except ProviderRequestError as exc:
            raise PublisherRemoteError(str(exc)) from None

    def _post_response(self, url: str, payload: Mapping[str, object]) -> HttpResponse:
        try:
            return self._http_client.post(url, headers=self._headers(), json=payload)
        except Exception:
            raise PublisherRemoteError("github request failed") from None

    @staticmethod
    def _response_json(response: HttpResponse) -> Any:
        try:
            return response.json()
        except Exception:
            raise PublisherRemoteError("github returned an invalid response") from None

    @staticmethod
    def _publish_result(
        payload: Mapping[str, Any], fork_url: str, branch: str, *, pr_created: bool
    ) -> PublishResult:
        url = payload.get("html_url")
        number = payload.get("number")
        state = payload.get("state")
        if (
            not isinstance(url, str)
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or state not in {"open", "closed"}
        ):
            raise PublisherRemoteError("github returned an invalid response")
        return PublishResult(
            url=url,
            number=number,
            state=cast(Any, state),
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
        state = payload.get("state")
        merged = payload.get("merged")
        base = payload.get("base")
        head = payload.get("head")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number != pr_number
            or not isinstance(url, str)
            or url != f"https://github.com/{owner}/{name}/pull/{pr_number}"
            or not isinstance(state, str)
            or state not in {"open", "closed"}
            or not isinstance(merged, bool)
            or not isinstance(base, dict)
            or not isinstance(head, dict)
        ):
            raise PublisherRemoteError("github returned an invalid pull request")

        base_sha = base.get("sha")
        head_sha = head.get("sha")
        base_ref = base.get("ref")
        head_ref = head.get("ref")
        head_repo = head.get("repo")
        if (
            not isinstance(base_sha, str)
            or _SHA_PATTERN.fullmatch(base_sha) is None
            or not isinstance(head_sha, str)
            or _SHA_PATTERN.fullmatch(head_sha) is None
            or not isinstance(base_ref, str)
            or not isinstance(head_ref, str)
            or not isinstance(head_repo, dict)
        ):
            raise PublisherRemoteError("github returned an invalid pull request")
        try:
            GitHubPublisher._validate_branch(base_ref)
            GitHubPublisher._validate_branch(head_ref)
            head_repository_url = GitHubPublisher._normalize_github_url(
                head_repo.get("clone_url")
            )
        except (InvalidPublisherInput, UnsupportedPublisher):
            raise PublisherRemoteError("github returned an invalid pull request") from None
        if str(head_repo.get("full_name", "")).lower() != head_repository_url.removeprefix(
            "https://github.com/"
        ).removesuffix(".git").lower():
            raise PublisherRemoteError("github returned an invalid pull request")
        return PullRequestDetails(
            number=number,
            url=url,
            state=cast(Any, state),
            merged=merged,
            base_sha=base_sha.lower(),
            head_sha=head_sha.lower(),
            base_ref=base_ref,
            head_ref=head_ref,
            head_repository_url=head_repository_url,
        )

    @staticmethod
    def _comment_url(value: object, owner: str, name: str, pr_number: int) -> str:
        if not isinstance(value, str):
            raise PublisherRemoteError("github returned an invalid pull request comment")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError):
            raise PublisherRemoteError("github returned an invalid pull request comment") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.path != f"/{owner}/{name}/pull/{pr_number}"
            or re.fullmatch(r"issuecomment-[0-9]+", parsed.fragment) is None
        ):
            raise PublisherRemoteError("github returned an invalid pull request comment")
        return value

    @staticmethod
    def _validate_name(value: str, label: str) -> None:
        if not isinstance(value, str) or _NAME_PATTERN.fullmatch(value) is None:
            raise InvalidPublisherInput(f"invalid {label}")

    @staticmethod
    def _validate_pr_number(pr_number: int) -> None:
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
            raise InvalidPublisherInput("pull request number must be positive")

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if (
            not isinstance(branch, str)
            or not branch
            or branch == "@"
            or branch.startswith("-")
            or branch.startswith("/")
            or branch.endswith(("/", "."))
            or ".." in branch
            or "@{" in branch
            or "//" in branch
            or _INVALID_REF_CHARACTERS.search(branch) is not None
            or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
        ):
            raise InvalidPublisherInput("invalid git branch name")

    @staticmethod
    def _normalize_github_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise InvalidPublisherInput("invalid github fork URL") from exc
        if parsed.hostname is not None and parsed.hostname.lower() != "github.com":
            raise UnsupportedPublisher("only github.com publishing is supported")
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidPublisherInput("invalid github fork URL")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            raise InvalidPublisherInput("invalid github fork URL")
        owner, name = parts
        if name.endswith(".git"):
            name = name[:-4]
        GitHubPublisher._validate_name(owner, "fork owner")
        GitHubPublisher._validate_name(name, "fork repository name")
        return f"https://github.com/{owner}/{name}.git"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
