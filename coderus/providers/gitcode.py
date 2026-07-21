from datetime import datetime
from typing import Any, Literal, cast

from .errors import ProviderRemoteError
from .http import HttpClient, default_http_client, get_json
from .models import Issue, ProviderName, Repository
from .urls import parse_issue_url, parse_repository_url


class GitCodeProvider:
    name: ProviderName = "gitcode"

    def __init__(self, *, client: HttpClient | None = None, token: str | None = None) -> None:
        self.client = client
        self.token = token or None

    def parse_repository_url(self, url: str) -> Repository:
        return parse_repository_url(url, expected_provider=self.name)

    def parse_issue_url(self, url: str) -> tuple[Repository, int]:
        return parse_issue_url(url, expected_provider=self.name)

    def get_repository(self, url: str) -> Repository:
        client = self._configured_client()
        repository = self.parse_repository_url(url)
        payload = get_json(
            client,
            self.name,
            self._api_url(repository),
            headers=self._headers(),
        )
        try:
            if not isinstance(payload, dict):
                raise TypeError("repository payload must be an object")
            default_branch = payload["default_branch"]
            private = payload.get("private")
            public = payload.get("public")
            issues_enabled = payload.get("has_issue", payload.get("has_issues"))
            if not isinstance(default_branch, str):
                raise TypeError("invalid repository fields")
            if issues_enabled is not None and not isinstance(issues_enabled, bool):
                raise TypeError("invalid issue capability")
            if not isinstance(private, bool) or not isinstance(public, bool):
                raise TypeError("invalid visibility fields")
        except (KeyError, TypeError) as exc:
            raise ProviderRemoteError(self.name, "gitcode returned an invalid response") from exc
        if private or not public:
            raise ProviderRemoteError(self.name, "gitcode repository is not public")
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
        client = self._configured_client()
        repository = self._validated_repository(repository)
        url = f"{self._api_url(repository)}/issues"
        issues: list[Issue] = []
        page = 1
        while True:
            payload = get_json(
                client,
                self.name,
                url,
                headers=self._headers(),
                params={
                    "state": state,
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(payload, list):
                raise ProviderRemoteError(self.name, "gitcode returned an invalid response")
            issues.extend(self._issue_from_payload(repository, item) for item in payload)
            if len(payload) < 100:
                return issues
            page += 1

    def get_issue(self, repository: Repository, number: int) -> Issue:
        client = self._configured_client()
        repository = self._validated_repository(repository)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("issue number must be a positive integer")
        payload = get_json(
            client,
            self.name,
            f"{self._api_url(repository)}/issues/{number}",
            headers=self._headers(),
        )
        return self._issue_from_payload(repository, payload)

    def _configured_client(self) -> HttpClient:
        if self.client is None:
            self.client = default_http_client()
        return self.client

    def _validated_repository(self, repository: Repository) -> Repository:
        parsed = self.parse_repository_url(repository.canonical_url)
        if (parsed.owner, parsed.name) != (repository.owner, repository.name):
            raise ValueError("repository fields do not match its canonical URL")
        return repository

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _api_url(repository: Repository) -> str:
        return f"https://api.gitcode.com/api/v5/repos/{repository.owner}/{repository.name}"

    def _issue_from_payload(self, repository: Repository, payload: Any) -> Issue:
        try:
            if not isinstance(payload, dict):
                raise TypeError("issue payload must be an object")
            number = self._issue_number(payload["number"])
            title = payload["title"]
            body = payload["body"]
            state = self._issue_state(payload["state"])
            labels = payload["labels"]
            if not isinstance(title, str) or body is not None and not isinstance(body, str):
                raise TypeError("invalid issue text")
            if not isinstance(labels, list):
                raise TypeError("invalid labels")
            label_names = tuple(self._label_name(label) for label in labels)
            external_id = str(payload["id"])
            created_at = self._parse_datetime(payload.get("created_at"))
            updated_at = self._parse_datetime(payload.get("updated_at"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderRemoteError(self.name, "gitcode returned an invalid response") from exc

        return Issue(
            repository=repository,
            external_id=external_id,
            number=number,
            title=title,
            body=body,
            state=state,
            labels=label_names,
            canonical_url=f"{repository.canonical_url}/issues/{number}",
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _issue_number(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError("invalid issue number")
        if isinstance(value, str) and not value.isdigit():
            raise ValueError("invalid issue number")
        number = int(value)
        if number < 1:
            raise ValueError("invalid issue number")
        return number

    @staticmethod
    def _issue_state(value: Any) -> Literal["open", "closed"]:
        if value in {"open", "opened"}:
            return "open"
        if value == "closed":
            return "closed"
        raise ValueError("invalid issue state")

    @staticmethod
    def _label_name(label: Any) -> str:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise TypeError("invalid label")
        return cast(str, label["name"])

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
