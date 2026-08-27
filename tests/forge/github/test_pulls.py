import os
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from coderus.forge import (
    ForkResult,
    GitCommandResult,
    GitHubPublisher,
    GitPushError,
    HttpsGitPusher,
    InvalidPublisherInput,
    PRFeedbackItem,
    PublisherRemoteError,
    PublishResult,
    RegisteredForkMismatch,
    UnsupportedPublisher,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeHttpClient:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, object] | None = None,
    ) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self.responses.popleft()

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
    ) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self.responses.popleft()


class FakeGitRunner:
    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> GitCommandResult:
        raise AssertionError("git must not run while checking a fork")


class RecordingGitRunner:
    def __init__(self, result: GitCommandResult | None = None) -> None:
        self.result = result or GitCommandResult(returncode=0)
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> GitCommandResult:
        if "push" not in args:
            if "--verify" in args:
                return GitCommandResult(returncode=0, stdout=f"{'a' * 40}\n")
            objects = cwd / ".git" / "objects"
            objects.mkdir(parents=True, exist_ok=True)
            return GitCommandResult(returncode=0, stdout=f"{objects.resolve()}\n")
        askpass = Path(env["GIT_ASKPASS"])
        self.calls.append(
            {
                "args": args,
                "cwd": cwd,
                "env": dict(env),
                "askpass": askpass.read_text(encoding="utf-8"),
            }
        )
        return self.result


def pull_request_payload() -> dict[str, Any]:
    return {
        "number": 7,
        "html_url": "https://github.com/acme/widgets/pull/7",
        "state": "open",
        "merged": False,
        "base": {
            "sha": "a" * 40,
            "ref": "main",
            "repo": {"full_name": "acme/widgets"},
        },
        "head": {
            "sha": "b" * 40,
            "ref": "feature/review",
            "repo": {
                "full_name": "contributor/widgets",
                "clone_url": "https://github.com/contributor/widgets.git",
            },
        },
    }


def mutate_pull_request_payload(path: tuple[str, ...], value: object) -> dict[str, Any]:
    payload = pull_request_payload()
    target: dict[str, Any] = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return payload


def test_ensure_fork_returns_existing_bot_fork() -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            {
                "fork": True,
                "clone_url": "https://github.com/coderus-bot/widgets.git",
                "owner": {"login": "coderus-bot"},
                "parent": {"full_name": "acme/widgets"},
            },
        ),
    )
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={},
        http_client=client,
        git_runner=FakeGitRunner(),
    )

    result = publisher.ensure_fork("acme", "widgets")

    assert result == ForkResult(
        url="https://github.com/coderus-bot/widgets.git",
        owner="coderus-bot",
        created=False,
    )
    assert [call["url"] for call in client.calls] == [
        "https://api.github.com/user",
        "https://api.github.com/repos/coderus-bot/widgets",
    ]
    assert all(call["headers"]["Authorization"] == "Bearer github-secret" for call in client.calls)


def test_ensure_fork_creates_and_polls_until_available() -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(404, {"message": "Not Found"}),
        FakeResponse(202, {"id": 123}),
        FakeResponse(404, {"message": "Not Found"}),
        FakeResponse(
            200,
            {
                "fork": True,
                "clone_url": "https://github.com/coderus-bot/widgets.git",
                "owner": {"login": "coderus-bot"},
                "parent": {"full_name": "acme/widgets"},
            },
        ),
    )
    sleeps: list[float] = []
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={},
        http_client=client,
        git_runner=FakeGitRunner(),
        sleep=sleeps.append,
        fork_poll_attempts=3,
        fork_poll_interval=0.25,
    )

    result = publisher.ensure_fork("acme", "widgets")

    assert result.created is True
    assert [call["method"] for call in client.calls] == ["GET", "GET", "POST", "GET", "GET"]
    assert client.calls[2]["url"] == "https://api.github.com/repos/acme/widgets/forks"
    assert client.calls[2]["json"] == {}
    assert sleeps == [0.25, 0.25]


def test_publish_pushes_registered_fork_and_uses_professional_pr_title(
    tmp_path: Path,
) -> None:
    token = "github-secret-value"
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            {
                "fork": True,
                "clone_url": "https://github.com/coderus-bot/widgets.git",
                "owner": {"login": "coderus-bot"},
                "parent": {"full_name": "acme/widgets"},
            },
        ),
        FakeResponse(200, []),
        FakeResponse(
            201,
            {
                "html_url": "https://github.com/acme/widgets/pull/17",
                "number": 17,
                "state": "open",
            },
        ),
    )
    git_runner = RecordingGitRunner()
    publisher = GitHubPublisher(
        token,
        registered_forks={("acme", "widgets"): "https://github.com/coderus-bot/widgets"},
        http_client=client,
        git_pusher=HttpsGitPusher(token, "x-access-token", git_runner=git_runner),
    )

    result = publisher.publish(
        tmp_path,
        "acme",
        "widgets",
        "main",
        "coderus/issue-42-task-7",
        "Repair the widget",
        "Fixes #42",
    )

    assert result == PublishResult(
        url="https://github.com/acme/widgets/pull/17",
        number=17,
        state="open",
        fork_url="https://github.com/coderus-bot/widgets.git",
        branch="coderus/issue-42-task-7",
        pr_created=True,
    )
    assert len(git_runner.calls) == 1
    git_call = git_runner.calls[0]
    assert git_call["args"][:1] == ("git",)
    assert git_call["args"][1].startswith("--git-dir=")
    assert git_call["args"][2:] == (
        "push",
        "--",
        "https://github.com/coderus-bot/widgets.git",
        f"{'a' * 40}:refs/heads/coderus/issue-42-task-7",
    )
    assert git_call["cwd"] != tmp_path
    assert git_call["env"]["CODERUS_GIT_TOKEN"] == token
    assert git_call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert git_call["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert git_call["env"]["GIT_CONFIG_GLOBAL"]
    assert git_call["env"]["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert git_call["env"]["GIT_CONFIG_VALUE_0"] == os.devnull
    assert token not in repr(git_call["args"])
    assert token not in git_call["askpass"]

    pr_query = client.calls[2]
    assert pr_query["url"] == "https://api.github.com/repos/acme/widgets/pulls"
    assert pr_query["params"] == {
        "state": "all",
        "head": "coderus-bot:coderus/issue-42-task-7",
        "base": "main",
        "per_page": 100,
    }
    assert client.calls[3]["json"] == {
        "title": "Repair the widget",
        "head": "coderus-bot:coderus/issue-42-task-7",
        "base": "main",
        "body": "Fixes #42",
        "draft": False,
    }


def test_publish_returns_existing_head_base_pr_without_creating_another(tmp_path: Path) -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            {
                "fork": True,
                "clone_url": "https://github.com/coderus-bot/widgets.git",
                "owner": {"login": "coderus-bot"},
                "parent": {"full_name": "acme/widgets"},
            },
        ),
        FakeResponse(
            200,
            [
                {
                    "html_url": "https://github.com/acme/widgets/pull/9",
                    "number": 9,
                    "state": "closed",
                }
            ],
        ),
    )
    git_runner = RecordingGitRunner()
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={("acme", "widgets"): "https://github.com/coderus-bot/widgets.git"},
        http_client=client,
        git_runner=git_runner,
    )

    result = publisher.publish(
        tmp_path, "acme", "widgets", "main", "coderus/task-9", "Widget", "Body"
    )

    assert result.url == "https://github.com/acme/widgets/pull/9"
    assert result.number == 9
    assert result.state == "closed"
    assert result.pr_created is False
    assert [call["method"] for call in client.calls] == ["GET", "GET", "GET"]


def test_list_pr_feedback_combines_comments_reviews_and_inline_comments() -> None:
    client = FakeHttpClient(
        FakeResponse(200, [{
            "id": 101,
            "body": "please add a test",
            "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-101",
            "user": {"login": "maintainer"},
            "author_association": "MEMBER",
        }]),
        FakeResponse(200, [{
            "id": 202,
            "body": "overall looks risky",
            "html_url": "https://github.com/acme/widgets/pull/7#pullrequestreview-202",
            "user": {"login": "reviewer"},
            "author_association": "COLLABORATOR",
        }]),
        FakeResponse(200, [{
            "id": 303,
            "body": "handle None here",
            "html_url": "https://github.com/acme/widgets/pull/7#discussion_r303",
            "user": {"login": "visitor"},
            "author_association": "NONE",
            "path": "src/widget.py",
            "line": 14,
        }]),
    )
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    result = publisher.list_pr_feedback("acme", "widgets", 7)

    assert result == [
        PRFeedbackItem(
            "issue_comment:101", "issue_comment", "maintainer", "MEMBER",
            "please add a test", "https://github.com/acme/widgets/pull/7#issuecomment-101",
        ),
        PRFeedbackItem(
            "review:202", "review", "reviewer", "COLLABORATOR",
            "overall looks risky", "https://github.com/acme/widgets/pull/7#pullrequestreview-202",
        ),
        PRFeedbackItem(
            "review_comment:303", "review_comment", "visitor", "NONE",
            "handle None here", "https://github.com/acme/widgets/pull/7#discussion_r303",
            "src/widget.py", 14,
        ),
    ]
    assert [call["url"] for call in client.calls] == [
        "https://api.github.com/repos/acme/widgets/issues/7/comments",
        "https://api.github.com/repos/acme/widgets/pulls/7/reviews",
        "https://api.github.com/repos/acme/widgets/pulls/7/comments",
    ]


def test_list_pr_feedback_reads_every_github_page() -> None:
    first_page = [
        {
            "id": identifier,
            "body": f"comment {identifier}",
            "html_url": f"https://github.com/acme/widgets/pull/7#issuecomment-{identifier}",
            "user": {"login": "maintainer"},
            "author_association": "MEMBER",
        }
        for identifier in range(1, 101)
    ]
    first = FakeResponse(200, first_page)
    first.headers = {
        "Link": (
            '<https://api.github.com/repos/acme/widgets/issues/7/comments?per_page=100&page=2>'
            '; rel="next"'
        )
    }
    client = FakeHttpClient(
        first,
        FakeResponse(200, [{**first_page[0], "id": 101}]),
        FakeResponse(200, []),
        FakeResponse(200, []),
    )
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    feedback = publisher.list_pr_feedback("acme", "widgets", 7)

    assert len(feedback) == 101
    assert [call["params"].get("page") for call in client.calls] == [1, 2, 1, 1]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"state": "open", "merged": False}, "open"),
        ({"state": "closed", "merged": False}, "closed"),
        ({"state": "closed", "merged": True}, "merged"),
    ],
)
def test_get_pr_status_distinguishes_merged_from_closed(payload, expected) -> None:
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={},
        http_client=FakeHttpClient(FakeResponse(200, payload)),
        git_runner=FakeGitRunner(),
    )

    assert publisher.get_pr_status("acme", "widgets", 7) == expected


def test_get_pull_request_returns_verified_metadata() -> None:
    client = FakeHttpClient(FakeResponse(200, pull_request_payload()))
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    result = publisher.get_pull_request("acme", "widgets", 7)

    assert result.number == 7
    assert result.url == "https://github.com/acme/widgets/pull/7"
    assert result.state == "open"
    assert result.merged is False
    assert result.base_sha == "a" * 40
    assert result.head_sha == "b" * 40
    assert result.base_ref == "main"
    assert result.head_ref == "feature/review"
    assert result.head_repository_url == "https://github.com/contributor/widgets.git"
    assert client.calls == [{
        "method": "GET",
        "url": "https://api.github.com/repos/acme/widgets/pulls/7",
        "headers": client.calls[0]["headers"],
        "params": None,
    }]


def test_publisher_exports_pull_request_result_models() -> None:
    from coderus.forge import PRCommentResult, PullRequestDetails

    assert PullRequestDetails.__name__ == "PullRequestDetails"
    assert PRCommentResult.__name__ == "PRCommentResult"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("number",), True, id="number-type"),
        pytest.param(("number",), 8, id="number-mismatch"),
        pytest.param(("html_url",), "https://github.com/acme/widgets/issues/7", id="pr-url"),
        pytest.param(
            ("html_url",), "https://token@github.com/acme/widgets/pull/7", id="pr-url-credentials"
        ),
        pytest.param(("html_url",), "https://gitcode.com/acme/widgets/pull/7", id="pr-url-host"),
        pytest.param(("state",), "merged", id="state"),
        pytest.param(("merged",), "false", id="merged"),
        pytest.param(("base", "sha"), "a" * 39, id="base-sha"),
        pytest.param(("head", "sha"), "not-a-sha", id="head-sha"),
        pytest.param(("base", "ref"), "--upload-pack=evil", id="base-ref"),
        pytest.param(("head", "ref"), "feature..review", id="head-ref"),
        pytest.param(("head", "repo", "full_name"), None, id="head-full-name-missing"),
        pytest.param(
            ("head", "repo", "full_name"), "contributor/other", id="head-full-name-mismatch"
        ),
        pytest.param(
            ("head", "repo", "clone_url"),
            "http://github.com/contributor/widgets.git",
            id="clone-url-http",
        ),
        pytest.param(
            ("head", "repo", "clone_url"),
            "https://token@github.com/contributor/widgets.git",
            id="clone-url-credentials",
        ),
        pytest.param(
            ("head", "repo", "clone_url"),
            "https://gitcode.com/contributor/widgets.git",
            id="clone-url-host",
        ),
        pytest.param(
            ("head", "repo", "clone_url"),
            "https://github.com/contributor/other.git",
            id="clone-url-mismatch",
        ),
    ],
)
def test_get_pull_request_rejects_single_invalid_metadata_field(
    path: tuple[str, ...], value: object
) -> None:
    payload = mutate_pull_request_payload(path, value)
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={},
        http_client=FakeHttpClient(FakeResponse(200, payload)),
        git_runner=FakeGitRunner(),
    )

    with pytest.raises(PublisherRemoteError):
        publisher.get_pull_request("acme", "widgets", 7)


def test_publish_pr_comment_reuses_existing_marker() -> None:
    marker = "<!-- coderus-pr-review:RV-4:abc -->"
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            [{
                "body": f"done\n{marker}",
                "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-9",
                "user": {"login": "coderus-bot"},
            }],
        )
    )
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    result = publisher.publish_pr_comment("acme", "widgets", 7, f"new body\n{marker}", marker)

    assert result.url == "https://github.com/acme/widgets/pull/7#issuecomment-9"
    assert result.created is False
    assert len(client.calls) == 2
    assert client.calls[0]["url"] == "https://api.github.com/user"
    assert client.calls[1]["params"] == {"per_page": 100, "page": 1}


def test_publish_pr_comment_reuses_marker_from_second_page() -> None:
    marker = "<!-- coderus-pr-review:RV-4:abc -->"
    first_page = [{"body": "unrelated comment"} for _ in range(100)]
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(200, first_page),
        FakeResponse(
            200,
            [{
                "body": f"done\n{marker}",
                "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-9",
                "user": {"login": "coderus-bot"},
            }],
        ),
    )
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    result = publisher.publish_pr_comment("acme", "widgets", 7, f"new body\n{marker}", marker)

    assert result.url == "https://github.com/acme/widgets/pull/7#issuecomment-9"
    assert result.created is False
    assert [call["method"] for call in client.calls] == ["GET", "GET", "GET"]
    assert [call["params"] for call in client.calls[1:]] == [
        {"per_page": 100, "page": 1},
        {"per_page": 100, "page": 2},
    ]


def test_publish_pr_comment_creates_comment_when_marker_is_not_a_suffix() -> None:
    marker = "<!-- coderus-pr-review:RV-4:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            [{
                "body": f"{marker}\nold review",
                "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-8",
            }],
        ),
        FakeResponse(
            201,
            {"html_url": "https://github.com/acme/widgets/pull/7#issuecomment-9"},
        ),
    )
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    result = publisher.publish_pr_comment("acme", "widgets", 7, body, marker)

    assert result.url == "https://github.com/acme/widgets/pull/7#issuecomment-9"
    assert result.created is True
    assert [call["method"] for call in client.calls] == ["GET", "GET", "POST"]
    assert client.calls[0]["url"] == "https://api.github.com/user"
    assert client.calls[1]["url"] == "https://api.github.com/repos/acme/widgets/issues/7/comments"
    assert client.calls[2]["url"] == "https://api.github.com/repos/acme/widgets/issues/7/comments"
    assert client.calls[2]["json"] == {"body": body}


def test_publish_pr_comment_does_not_reuse_marker_from_another_author() -> None:
    marker = "<!-- coderus-pr-review:random-key:abc -->"
    body = f"review complete\n{marker}"
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            [{
                "body": f"forged\n{marker}",
                "html_url": "https://github.com/acme/widgets/pull/7#issuecomment-8",
                "user": {"login": "attacker"},
            }],
        ),
        FakeResponse(
            201,
            {"html_url": "https://github.com/acme/widgets/pull/7#issuecomment-9"},
        ),
    )
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    result = publisher.publish_pr_comment("acme", "widgets", 7, body, marker)

    assert result.created is True
    assert result.url.endswith("#issuecomment-9")
    assert [call["method"] for call in client.calls] == ["GET", "GET", "POST"]


def test_publish_pr_comment_rejects_body_without_marker_suffix_before_http() -> None:
    client = FakeHttpClient()
    publisher = GitHubPublisher(
        "github-secret", registered_forks={}, http_client=client, git_runner=FakeGitRunner()
    )

    with pytest.raises(InvalidPublisherInput, match="marker"):
        publisher.publish_pr_comment(
            "acme", "widgets", 7, "review complete", "<!-- coderus-pr-review:RV-4:abc -->"
        )

    assert client.calls == []


@pytest.mark.parametrize("failure", ["invalid_response", "http_status", "transport"])
def test_publish_pr_comment_failure_does_not_expose_token(failure: str) -> None:
    token = "github-secret-value"
    marker = "<!-- coderus-pr-review:RV-4:abc -->"

    class ExplodingPostClient:
        def get(self, url: str, *args: object, **kwargs: object) -> FakeResponse:
            if url == "https://api.github.com/user":
                return FakeResponse(200, {"login": "coderus-bot"})
            return FakeResponse(200, [])

        def post(self, *args: object, **kwargs: object) -> FakeResponse:
            raise OSError(f"authorization included {token}")

    if failure == "transport":
        client = ExplodingPostClient()
    else:
        response = (
            FakeResponse(201, {"html_url": "https://example.com/comment/9"})
            if failure == "invalid_response"
            else FakeResponse(500, {"message": f"token was {token}"})
        )
        client = FakeHttpClient(
            FakeResponse(200, {"login": "coderus-bot"}),
            FakeResponse(200, []),
            response,
        )
    publisher = GitHubPublisher(
        token,
        registered_forks={},
        http_client=client,
        git_runner=FakeGitRunner(),
    )

    with pytest.raises(PublisherRemoteError) as error:
        publisher.publish_pr_comment("acme", "widgets", 7, f"review\n{marker}", marker)

    assert token not in str(error.value)
    assert token not in repr(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "--force",
        "../main",
        "feature:main",
        "feature lock",
        "feature@{upstream}",
        "feature//nested",
        ".hidden/main",
        "feature.lock/main",
        "feature/main.lock",
        "feature\\main",
    ],
)
def test_publish_rejects_invalid_branch_before_network_or_git(tmp_path: Path, branch: str) -> None:
    client = FakeHttpClient()
    git_runner = RecordingGitRunner()
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={("acme", "widgets"): "https://github.com/coderus-bot/widgets.git"},
        http_client=client,
        git_runner=git_runner,
    )

    with pytest.raises(InvalidPublisherInput, match="branch"):
        publisher.publish(tmp_path, "acme", "widgets", "main", branch, "Widget", "Body")

    assert client.calls == []
    assert git_runner.calls == []


def test_publish_refuses_fork_that_does_not_match_registration(tmp_path: Path) -> None:
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            {
                "fork": True,
                "clone_url": "https://github.com/coderus-bot/widgets.git",
                "owner": {"login": "coderus-bot"},
                "parent": {"full_name": "acme/widgets"},
            },
        ),
    )
    git_runner = RecordingGitRunner()
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={("acme", "widgets"): "https://github.com/different-bot/widgets.git"},
        http_client=client,
        git_runner=git_runner,
    )

    with pytest.raises(RegisteredForkMismatch):
        publisher.publish(
            tmp_path, "acme", "widgets", "main", "coderus/task-1", "Widget", "Body"
        )

    assert git_runner.calls == []


def test_non_github_registered_url_is_explicitly_unsupported() -> None:
    with pytest.raises(UnsupportedPublisher, match="github.com"):
        GitHubPublisher(
            "github-secret",
            registered_forks={
                ("acme", "widgets"): "https://gitcode.com/coderus-bot/widgets.git"
            },
            http_client=FakeHttpClient(),
            git_runner=FakeGitRunner(),
        )


def test_git_failure_never_exposes_token_in_error(tmp_path: Path) -> None:
    token = "github-secret-value"
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            {
                "fork": True,
                "clone_url": "https://github.com/coderus-bot/widgets.git",
                "owner": {"login": "coderus-bot"},
                "parent": {"full_name": "acme/widgets"},
            },
        ),
    )
    git_runner = RecordingGitRunner(
        GitCommandResult(returncode=128, stderr=f"authentication failed for {token}")
    )
    publisher = GitHubPublisher(
        token,
        registered_forks={("acme", "widgets"): "https://github.com/coderus-bot/widgets.git"},
        http_client=client,
        git_runner=git_runner,
    )

    with pytest.raises(GitPushError) as error:
        publisher.publish(
            tmp_path, "acme", "widgets", "main", "coderus/task-1", "Widget", "Body"
        )

    assert token not in str(error.value)
    assert token not in repr(error.value)
    assert error.value.__cause__ is None


def test_constructor_supplies_default_clients_without_exposing_token() -> None:
    publisher = GitHubPublisher("github-secret", registered_forks={})

    assert "github-secret" not in repr(publisher)


def test_ensure_fork_rejects_unsafe_repository_coordinates_before_http() -> None:
    client = FakeHttpClient()
    publisher = GitHubPublisher(
        "github-secret",
        registered_forks={},
        http_client=client,
        git_runner=FakeGitRunner(),
    )

    with pytest.raises(InvalidPublisherInput):
        publisher.ensure_fork("acme/../../users", "widgets")

    assert client.calls == []


def test_http_exception_is_wrapped_without_token_or_exception_chain() -> None:
    token = "github-secret-value"

    class ExplodingHttpClient:
        def get(self, *args: object, **kwargs: object) -> FakeResponse:
            raise OSError(f"request headers included Bearer {token}")

        def post(self, *args: object, **kwargs: object) -> FakeResponse:
            raise AssertionError("post must not run")

    publisher = GitHubPublisher(
        token,
        registered_forks={},
        http_client=ExplodingHttpClient(),
        git_runner=FakeGitRunner(),
    )

    with pytest.raises(PublisherRemoteError) as error:
        publisher.ensure_fork("acme", "widgets")

    assert token not in str(error.value)
    assert token not in repr(error.value)
    assert error.value.__cause__ is None


def test_git_exception_is_wrapped_without_token_or_exception_chain(tmp_path: Path) -> None:
    token = "github-secret-value"
    client = FakeHttpClient(
        FakeResponse(200, {"login": "coderus-bot"}),
        FakeResponse(
            200,
            {
                "fork": True,
                "clone_url": "https://github.com/coderus-bot/widgets.git",
                "owner": {"login": "coderus-bot"},
                "parent": {"full_name": "acme/widgets"},
            },
        ),
    )

    class ExplodingGitRunner:
        def run(
            self,
            args: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str],
        ) -> GitCommandResult:
            if "push" not in args:
                if "--verify" in args:
                    return GitCommandResult(returncode=0, stdout=f"{'a' * 40}\n")
                objects = cwd / ".git" / "objects"
                objects.mkdir(parents=True, exist_ok=True)
                return GitCommandResult(returncode=0, stdout=f"{objects.resolve()}\n")
            raise OSError(f"git environment included {token}")

    publisher = GitHubPublisher(
        token,
        registered_forks={("acme", "widgets"): "https://github.com/coderus-bot/widgets.git"},
        http_client=client,
        git_runner=ExplodingGitRunner(),
    )

    with pytest.raises(GitPushError) as error:
        publisher.publish(
            tmp_path, "acme", "widgets", "main", "coderus/task-1", "Widget", "Body"
        )

    assert token not in str(error.value)
    assert token not in repr(error.value)
    assert error.value.__cause__ is None
