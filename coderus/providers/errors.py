class ProviderError(Exception):
    """Base class for code-hosting provider failures."""


class InvalidProviderUrl(ProviderError, ValueError):
    """Raised when a repository or issue URL is outside the supported format."""


class ProviderNotConfiguredError(ProviderError):
    """Raised when an operation needs provider configuration that is missing."""


class ProviderRemoteError(ProviderError):
    """Raised when a provider request fails or returns an invalid response."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after
