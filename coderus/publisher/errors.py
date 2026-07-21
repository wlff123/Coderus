class PublisherError(Exception):
    """Base class for controlled publishing failures."""


class UnsupportedPublisher(PublisherError):
    """Raised when a registered remote is not hosted on github.com."""


class InvalidPublisherInput(PublisherError, ValueError):
    """Raised when caller-provided publishing input is unsafe or invalid."""


class RegisteredForkMismatch(PublisherError):
    """Raised when the authenticated fork differs from the registered fork."""


class GitPushError(PublisherError):
    """Raised when the controlled git push fails."""


class PublisherRemoteError(PublisherError):
    """Raised when GitHub returns an error or invalid response."""


class ForkNotReady(PublisherRemoteError):
    """Raised when a newly created fork does not become available in time."""
