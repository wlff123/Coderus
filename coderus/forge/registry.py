from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from coderus.providers import ProviderName


class ForgeNotConfigured(ValueError):
    _PLATFORM_NAMES = {"github": "GitHub", "gitcode": "GitCode"}

    def __init__(self, provider: ProviderName) -> None:
        super().__init__(f"{self._PLATFORM_NAMES[provider]} 平台尚未配置")


class ForgeCapability(StrEnum):
    ENSURE_FORK = "ensure_fork"
    PUBLISH = "publish"
    LIST_PR_FEEDBACK = "list_pr_feedback"
    GET_PR_STATUS = "get_pr_status"
    GET_PULL_REQUEST = "get_pull_request"
    PUBLISH_PR_COMMENT = "publish_pr_comment"


ALL_FORGE_CAPABILITIES = frozenset(ForgeCapability)


@dataclass(frozen=True, slots=True)
class ForgeRegistration:
    forge: object | None
    capabilities: frozenset[ForgeCapability]

    def __post_init__(self) -> None:
        capabilities = frozenset(self.capabilities)
        if not all(isinstance(item, ForgeCapability) for item in capabilities):
            raise TypeError("forge capabilities must use ForgeCapability values")
        if self.forge is None and capabilities:
            raise ValueError("an unavailable forge cannot expose capabilities")
        object.__setattr__(self, "capabilities", capabilities)

    @classmethod
    def full(cls, forge: object) -> ForgeRegistration:
        return cls(forge=forge, capabilities=ALL_FORGE_CAPABILITIES)

    @classmethod
    def unavailable(cls) -> ForgeRegistration:
        return cls(forge=None, capabilities=frozenset())

    def supports(self, *capabilities: ForgeCapability) -> bool:
        return self.forge is not None and all(
            capability in self.capabilities for capability in capabilities
        )


class ForgeRegistry:
    def __init__(
        self,
        initial: Mapping[ProviderName, object | ForgeRegistration] | None = None,
    ) -> None:
        self._registrations: Mapping[ProviderName, ForgeRegistration] = MappingProxyType(
            {
                provider: self._normalize(registration)
                for provider, registration in (initial or {}).items()
            }
        )

    def install(
        self,
        provider: ProviderName,
        forge: object,
        *,
        capabilities: frozenset[ForgeCapability] = ALL_FORGE_CAPABILITIES,
    ) -> None:
        self.install_registration(provider, ForgeRegistration(forge, capabilities))

    def install_registration(
        self, provider: ProviderName, registration: ForgeRegistration
    ) -> None:
        updated = dict(self._registrations)
        updated[provider] = registration
        self._registrations = MappingProxyType(updated)

    def get(self, provider: ProviderName) -> object | None:
        registration = self._registrations.get(provider)
        return registration.forge if registration is not None else None

    def require(self, provider: ProviderName) -> object:
        forge = self.get(provider)
        if forge is None:
            raise ForgeNotConfigured(provider)
        return forge

    def configured(self, provider: ProviderName) -> bool:
        return provider in self._registrations

    def supports(
        self, provider: ProviderName, *capabilities: ForgeCapability
    ) -> bool:
        registration = self._registrations.get(provider)
        return registration is not None and registration.supports(*capabilities)

    def snapshot(self) -> Mapping[ProviderName, ForgeRegistration]:
        return self._registrations

    def remove(self, provider: ProviderName) -> None:
        if provider not in self._registrations:
            return
        updated = dict(self._registrations)
        updated.pop(provider)
        self._registrations = MappingProxyType(updated)

    @staticmethod
    def _normalize(value: object | ForgeRegistration) -> ForgeRegistration:
        if isinstance(value, ForgeRegistration):
            return value
        return ForgeRegistration.full(value)
