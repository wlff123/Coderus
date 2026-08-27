import re
from urllib.parse import urlsplit

from coderus.forge.errors import InvalidProviderUrl
from coderus.forge.models import ProviderName, Repository

_HOSTS: dict[str, ProviderName] = {
    "github.com": "github",
    "gitcode.com": "gitcode",
}
_GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_GITCODE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def parse_repository_url(url: str, *, expected_provider: ProviderName | None = None) -> Repository:
    provider, host, parts = _parse_public_url(url)
    if expected_provider is not None and provider != expected_provider:
        raise InvalidProviderUrl(f"expected a {expected_provider} URL")
    if len(parts) != 2:
        raise InvalidProviderUrl("repository URL must contain exactly owner and repository")

    owner, name = parts
    _validate_repository_parts(provider, owner, name)
    return Repository(
        provider=provider,
        owner=owner,
        name=name,
        canonical_url=f"https://{host}/{owner}/{name}",
    )


def parse_issue_url(
    url: str, *, expected_provider: ProviderName | None = None
) -> tuple[Repository, int]:
    provider, host, parts = _parse_public_url(url)
    if expected_provider is not None and provider != expected_provider:
        raise InvalidProviderUrl(f"expected a {expected_provider} URL")
    if len(parts) != 4 or parts[2] != "issues" or not parts[3].isdigit():
        raise InvalidProviderUrl("issue URL must end with /issues/<positive number>")

    owner, name, _, raw_number = parts
    _validate_repository_parts(provider, owner, name)
    number = int(raw_number)
    if number < 1:
        raise InvalidProviderUrl("issue number must be positive")

    repository = Repository(
        provider=provider,
        owner=owner,
        name=name,
        canonical_url=f"https://{host}/{owner}/{name}",
    )
    return repository, number


def parse_pull_request_url(url: str) -> tuple[Repository, int]:
    provider, host, parts = _parse_public_url(url)
    coordinates = {"pull"} if provider == "github" else {
        "pull",
        "pulls",
        "merge_requests",
    }
    if len(parts) != 4 or parts[2] not in coordinates or not parts[3].isdigit():
        raise InvalidProviderUrl(
            "pull request URL does not use a supported path"
        )

    owner, name, _, raw_number = parts
    _validate_repository_parts(provider, owner, name)
    number = int(raw_number)
    if number < 1:
        raise InvalidProviderUrl("pull request number must be positive")

    repository = Repository(
        provider=provider,
        owner=owner,
        name=name,
        canonical_url=f"https://{host}/{owner}/{name}",
    )
    return repository, number


def _parse_public_url(url: str) -> tuple[ProviderName, str, list[str]]:
    if not isinstance(url, str) or not url or url != url.strip():
        raise InvalidProviderUrl("URL must be a non-empty string without surrounding whitespace")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url
    ):
        raise InvalidProviderUrl("whitespace and control characters are not allowed")
    if "%" in url or "\\" in url:
        raise InvalidProviderUrl("encoded or backslash path separators are not allowed")

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise InvalidProviderUrl("URL is malformed") from exc

    host = parsed.hostname
    if parsed.scheme != "https":
        raise InvalidProviderUrl("only HTTPS URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidProviderUrl("credential-bearing URLs are not allowed")
    if port is not None:
        raise InvalidProviderUrl("explicit ports are not allowed")
    if host not in _HOSTS or parsed.netloc.lower() != host:
        raise InvalidProviderUrl("host must be exactly github.com or gitcode.com")
    if parsed.query or parsed.fragment:
        raise InvalidProviderUrl("query strings and fragments are not allowed")

    raw_parts = parsed.path.split("/")[1:]
    if raw_parts and raw_parts[-1] == "":
        raw_parts.pop()
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise InvalidProviderUrl("URL path contains an empty or traversal segment")
    return _HOSTS[host], host, raw_parts


def _validate_repository_parts(provider: ProviderName, owner: str, name: str) -> None:
    if name.endswith(".git") or name in {".", ".."}:
        raise InvalidProviderUrl("clone URLs and traversal names are not supported")
    if provider == "github":
        valid = _GITHUB_OWNER.fullmatch(owner) and _GITHUB_REPOSITORY.fullmatch(name)
    else:
        valid = _GITCODE_SEGMENT.fullmatch(owner) and _GITCODE_SEGMENT.fullmatch(name)
    if not valid:
        raise InvalidProviderUrl("owner or repository contains unsupported characters")
