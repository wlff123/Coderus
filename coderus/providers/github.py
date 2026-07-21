import re
from datetime import datetime
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from .errors import ProviderRemoteError
from .http import HttpClient, default_http_client, get_json, get_json_response
from .models import Issue, ProviderName, Repository
from .urls import parse_issue_url, parse_repository_url


class GitHubProvider:
    name: ProviderName = "github"
    _MAX_ISSUE_PAGES = 1000

    def __init__(self, *, client: HttpClient | None = None, token: str | None = None) -> None:
        self.client = client if client is not None else default_http_client()
        self.token = token or None

    def parse_repository_url(self, url: str) -> Repository:
        return parse_repository_url(url, expected_provider=self.name)

    def parse_issue_url(self, url: str) -> tuple[Repository, int]:
        return parse_issue_url(url, expected_provider=self.name)

    def get_repository(self, url: str) -> Repository:
        repository = self.parse_repository_url(url)
        payload = get_json(
            self.client,
            self.name,
            self._api_url(repository),
            headers=self._headers(),
        )
        try:
            private = payload["private"]
            default_branch = payload["default_branch"]
            issues_enabled = payload["has_issues"]
            if not isinstance(private, bool):
                raise TypeError("private must be bool")
            if not isinstance(default_branch, str) or not isinstance(issues_enabled, bool):
                raise TypeError("invalid repository fields")
        except (KeyError, TypeError) as exc:
            raise ProviderRemoteError(self.name, "github returned an invalid response") from exc
        if private:
            raise ProviderRemoteError(self.name, "github repository is not public")
        return Repository(
            provider=self.name,
            owner=repository.owner,
            name=repository.name,
            canonical_url=repository.canonical_url,
            default_branch=default_branch,
            is_private=False,
            issues_enabled=issues_enabled,
        )

    def list_open_issues(self, repository: Repository) -> list[Issue]:
        return self.list_issues(repository, state="open")

    def list_issues(self, repository: Repository, *, state: str = "all") -> list[Issue]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("issue state must be open, closed, or all")
        repository = self._validated_repository(repository)
        url = f"{self._api_url(repository)}/issues"
        issues: list[Issue] = []
        page = 1
        after: str | None = None
        visited_pages: set[int] = set()
        while True:
            if page in visited_pages or len(visited_pages) >= self._MAX_ISSUE_PAGES:
                raise ProviderRemoteError(self.name, "github returned invalid pagination")
            visited_pages.add(page)
            params: dict[str, object] = {
                "state": state,
                "per_page": 100,
                "page": page,
            }
            if after is not None:
                params["after"] = after
            payload, headers = get_json_response(
                self.client,
                self.name,
                url,
                headers=self._headers(),
                params=params,
            )
            if not isinstance(payload, list):
                raise ProviderRemoteError(self.name, "github returned an invalid response")
            for item in payload:
                if isinstance(item, dict) and "pull_request" in item:
                    continue
                issues.append(self._issue_from_payload(repository, item))
            next_pagination = self._next_page(
                headers,
                expected_url=url,
                expected_state=state,
                current_page=page,
            )
            if next_pagination is None:
                return issues
            if len(visited_pages) >= self._MAX_ISSUE_PAGES:
                raise ProviderRemoteError(self.name, "github returned invalid pagination")
            page, after = next_pagination

    def get_issue(self, repository: Repository, number: int) -> Issue:
        repository = self._validated_repository(repository)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("issue number must be a positive integer")
        payload = get_json(
            self.client,
            self.name,
            f"{self._api_url(repository)}/issues/{number}",
            headers=self._headers(),
        )
        if isinstance(payload, dict) and "pull_request" in payload:
            raise ProviderRemoteError(self.name, "github item is a pull request, not an issue")
        return self._issue_from_payload(repository, payload)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _next_page(
        headers: Any,
        *,
        expected_url: str,
        expected_state: str,
        current_page: int,
    ) -> tuple[int, str | None] | None:
        link_header = next(
            (value for key, value in headers.items() if key.lower() == "link"),
            "",
        )
        next_urls: list[str] = []
        for link in link_header.split(","):
            if 'rel="next"' not in link:
                continue
            start = link.find("<")
            end = link.find(">", start + 1)
            if start < 0 or end < 0:
                raise ProviderRemoteError("github", "github returned invalid pagination")
            next_urls.append(link[start + 1 : end])
        if not next_urls:
            return None
        if len(next_urls) != 1:
            raise ProviderRemoteError("github", "github returned invalid pagination")

        try:
            parsed = urlsplit(next_urls[0])
            port = parsed.port
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except (TypeError, ValueError):
            raise ProviderRemoteError("github", "github returned invalid pagination") from None
        expected_path = urlsplit(expected_url).path
        canonical_path = re.fullmatch(r"/repositories/[1-9][0-9]*/issues", parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.github.com"
            or parsed.netloc != "api.github.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.fragment
            or (parsed.path != expected_path and canonical_path is None)
            or set(query) - {"state", "per_page", "page", "after"}
            or any(len(values) != 1 for values in query.values())
            or query.get("state", [expected_state])[0] != expected_state
            or query.get("per_page", ["100"])[0] != "100"
        ):
            raise ProviderRemoteError("github", "github returned invalid pagination")
        raw_page = query.get("page", [None])[0]
        if not isinstance(raw_page, str) or not raw_page.isdigit():
            raise ProviderRemoteError("github", "github returned invalid pagination")
        page = int(raw_page)
        if page < 1 or page <= current_page:
            raise ProviderRemoteError("github", "github returned invalid pagination")
        after = query.get("after", [None])[0]
        if after is not None and (
            not after
            or len(after) > 2048
            or any(ord(character) < 33 or ord(character) > 126 for character in after)
        ):
            raise ProviderRemoteError("github", "github returned invalid pagination")
        return page, after

    def _validated_repository(self, repository: Repository) -> Repository:
        parsed = self.parse_repository_url(repository.canonical_url)
        if (parsed.owner, parsed.name) != (repository.owner, repository.name):
            raise ValueError("repository fields do not match its canonical URL")
        return repository

    @staticmethod
    def _api_url(repository: Repository) -> str:
        return f"https://api.github.com/repos/{repository.owner}/{repository.name}"

    def _issue_from_payload(self, repository: Repository, payload: Any) -> Issue:
        try:
            if not isinstance(payload, dict):
                raise TypeError("issue payload must be an object")
            number = payload["number"]
            title = payload["title"]
            body = payload["body"]
            state = payload["state"]
            labels = payload["labels"]
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise TypeError("invalid issue number")
            if not isinstance(title, str) or body is not None and not isinstance(body, str):
                raise TypeError("invalid issue text")
            if state not in {"open", "closed"} or not isinstance(labels, list):
                raise TypeError("invalid issue state or labels")
            label_names = tuple(self._label_name(label) for label in labels)
            external_id = str(payload["id"])
            created_at = self._parse_datetime(payload.get("created_at"))
            updated_at = self._parse_datetime(payload.get("updated_at"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderRemoteError(self.name, "github returned an invalid response") from exc

        return Issue(
            repository=repository,
            external_id=external_id,
            number=number,
            title=title,
            body=body,
            state=cast(Any, state),
            labels=label_names,
            canonical_url=f"{repository.canonical_url}/issues/{number}",
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _label_name(label: Any) -> str:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise TypeError("invalid label")
        return label["name"]

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("timestamp must be a string")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return parsed
