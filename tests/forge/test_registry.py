import pytest

from coderus.forge import ForgeCapability, ForgeNotConfigured, ForgeRegistration, ForgeRegistry


def test_registry_returns_none_for_unconfigured_forge() -> None:
    registry = ForgeRegistry()

    assert registry.get("github") is None
    assert registry.configured("github") is False
    with pytest.raises(ForgeNotConfigured, match="GitHub"):
        registry.require("github")


def test_registry_replaces_installed_forge() -> None:
    registry = ForgeRegistry()
    first = object()
    second = object()

    registry.install("github", first)
    assert registry.require("github") is first
    registry.install("github", second)
    assert registry.require("github") is second
    assert registry.get("gitcode") is None


def test_registry_uses_typed_capabilities_and_copy_on_write_snapshots() -> None:
    forge = object()
    registry = ForgeRegistry(
        {
            "github": ForgeRegistration(
                forge, frozenset({ForgeCapability.GET_PULL_REQUEST})
            )
        }
    )
    original = registry.snapshot()

    registry.install_registration("gitcode", ForgeRegistration.unavailable())

    assert registry.supports("github", ForgeCapability.GET_PULL_REQUEST)
    assert not registry.supports("github", ForgeCapability.PUBLISH_PR_COMMENT)
    assert registry.configured("gitcode")
    assert registry.get("gitcode") is None
    assert "gitcode" not in original
    assert registry.snapshot() is not original
