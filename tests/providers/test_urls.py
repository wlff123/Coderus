import pytest

from coderus.providers import InvalidProviderUrl, parse_issue_url, parse_repository_url
from coderus.providers.urls import parse_pull_request_url


@pytest.mark.parametrize(
    ("url", "provider", "owner", "name"),
    [
        ("https://github.com/octocat/Hello-World", "github", "octocat", "Hello-World"),
        ("https://github.com/octocat/Hello-World/", "github", "octocat", "Hello-World"),
        ("https://gitcode.com/example/project", "gitcode", "example", "project"),
    ],
)
def test_parse_repository_url_returns_canonical_repository(
    url: str, provider: str, owner: str, name: str
) -> None:
    repository = parse_repository_url(url)

    assert repository.provider == provider
    assert repository.owner == owner
    assert repository.name == name
    assert repository.canonical_url == f"https://{provider}.com/{owner}/{name}"


@pytest.mark.parametrize(
    ("url", "provider", "number"),
    [
        ("https://github.com/octocat/Hello-World/issues/42", "github", 42),
        ("https://gitcode.com/example/project/issues/9/", "gitcode", 9),
    ],
)
def test_parse_issue_url_returns_repository_and_number(
    url: str, provider: str, number: int
) -> None:
    repository, parsed_number = parse_issue_url(url)

    assert repository.provider == provider
    assert parsed_number == number


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/octocat/Hello-World",
        "git://github.com/octocat/Hello-World",
        "https://user:secret@github.com/octocat/Hello-World",
        "https://github.com:443/octocat/Hello-World",
        "https://www.github.com/octocat/Hello-World",
        "https://example.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World?tab=readme",
        "https://github.com/octocat/Hello-World#readme",
        "https://github.com/octocat/../Hello-World",
        "https://github.com/octocat/%2e%2e",
        "https://github.com/octocat%2fadmin/Hello-World",
        "https://github.com/octocat\\admin/Hello-World",
        "https://github.com/octocat/Hello-World.git",
        "https://github.com/octocat/Hello-\nWorld",
        "https://github.com/octocat/Hello-\tWorld",
        " https://github.com/octocat/Hello-World",
    ],
)
def test_parse_repository_url_rejects_non_public_or_ambiguous_forms(url: str) -> None:
    with pytest.raises(InvalidProviderUrl):
        parse_repository_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat/Hello-World/pull/42",
        "https://github.com/octocat/Hello-World/issues/0",
        "https://github.com/octocat/Hello-World/issues/-1",
        "https://github.com/octocat/Hello-World/issues/1/comments",
        "https://gitcode.com/example/project/-/issues/1",
        "https://gitcode.com/example/project/issues/%31",
    ],
)
def test_parse_issue_url_rejects_non_issue_and_encoded_paths(url: str) -> None:
    with pytest.raises(InvalidProviderUrl):
        parse_issue_url(url)


def test_parse_github_pull_request_url() -> None:
    repository, number = parse_pull_request_url(
        "https://github.com/acme/widgets/pull/17"
    )

    assert (repository.provider, repository.owner, repository.name, number) == (
        "github",
        "acme",
        "widgets",
        17,
    )


@pytest.mark.parametrize("coordinate", ["pull", "pulls", "merge_requests"])
def test_parse_gitcode_pull_request_url(coordinate: str) -> None:
    repository, number = parse_pull_request_url(
        f"https://gitcode.com/acme/widgets/{coordinate}/17"
    )

    assert (repository.provider, repository.owner, repository.name, number) == (
        "gitcode",
        "acme",
        "widgets",
        17,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://gitcode.com/acme/widgets/pulls/0",
        "https://gitcode.com/acme/widgets/pulls/17/comments",
        "https://gitcode.com/acme/widgets/merge_requests/17/comments",
        "https://gitcode.com/acme/widgets/pulls/17?tab=files",
        "https://gitcode.com/acme/widgets/pulls/17#discussion",
        "https://gitcode.com:443/acme/widgets/pulls/17",
        "https://user:secret@gitcode.com/acme/widgets/pulls/17",
        "https://gitcode.com/acme/widgets/pulls/%31%37",
        "https://gitcode.com/acme/../widgets/pulls/17",
        "https://github.com/acme/widgets/pulls/17",
        "https://github.com/acme/widgets/pull/0",
        "https://github.com/acme/widgets/pull/17/comments",
        "https://github.com/acme/widgets/pull/17?tab=files",
        "https://github.com/acme/widgets/pull/17#discussion",
        "https://github.com:443/acme/widgets/pull/17",
        "https://user:secret@github.com/acme/widgets/pull/17",
    ],
)
def test_parse_pull_request_url_rejects_non_canonical_forms(url: str) -> None:
    with pytest.raises(InvalidProviderUrl):
        parse_pull_request_url(url)
