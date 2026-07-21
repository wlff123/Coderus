from __future__ import annotations

from dataclasses import dataclass

from coderus.forge import ForgeRegistration, GitCodeForge, GitHubForge
from coderus.providers import GitCodeProvider, GitHubProvider, ProviderName


@dataclass(frozen=True)
class ForgeRuntime:
    provider_client: object
    registration: ForgeRegistration


def build_github_runtime(
    token: str,
    *,
    client: object,
    session_factory,
) -> ForgeRuntime:
    return ForgeRuntime(
        provider_client=GitHubProvider(client=client, token=token),
        registration=ForgeRegistration.full(
            GitHubForge(token, session_factory=session_factory)
        ),
    )


def build_gitcode_runtime(
    token: str,
    *,
    account_name: str | None,
    client: object,
    session_factory,
) -> ForgeRuntime:
    return ForgeRuntime(
        provider_client=GitCodeProvider(client=client, token=token),
        registration=(
            ForgeRegistration.full(
                GitCodeForge(
                    token,
                    account_name,
                    session_factory=session_factory,
                    http_client=client,
                )
            )
            if account_name is not None
            else ForgeRegistration.unavailable()
        ),
    )


def install_forge_runtime(app, provider: ProviderName, runtime: ForgeRuntime) -> None:
    app.state.providers[provider] = runtime.provider_client
    if app.state.issue_poller.providers is not app.state.providers:
        app.state.issue_poller.providers[provider] = runtime.provider_client
    app.state.forges.install_registration(provider, runtime.registration)
