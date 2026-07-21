from base64 import urlsafe_b64decode
from pathlib import Path

import pytest
from pydantic import SecretStr

from coderus.cli import (
    build_parser,
    initialize_local,
    load_env_file,
    prepare_runtime_settings,
    resolve_runtime_paths,
)
from coderus.config import DatabaseSettings, Settings


def test_initialize_local_creates_config_and_secret_file(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    secrets = tmp_path / "secrets.env"

    password = initialize_local(config, secrets, admin_password="admin-password")

    assert password == "admin-password"
    assert "mode: local" in config.read_text(encoding="utf-8")
    loaded = load_env_file(secrets)
    assert loaded["CODERUS_BOOTSTRAP_ADMIN_PASSWORD"] == "admin-password"
    assert len(loaded["CODERUS_SESSION_SECRET"]) >= 32
    assert len(urlsafe_b64decode(loaded["CODERUS_CREDENTIAL_ENCRYPTION_KEY"])) == 32


def test_serve_parser_accepts_runtime_and_port_override() -> None:
    args = build_parser().parse_args(
        ["serve", "--runtime", "preview", "--port", "18084"]
    )

    assert args.runtime == "preview"
    assert args.port == 18084


def test_serve_parser_rejects_unknown_runtime() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["serve", "--runtime", "unknown"])


def test_runtime_paths_are_resolved_from_config_directory(tmp_path: Path) -> None:
    settings = Settings(
        database=DatabaseSettings(path=Path("data/coderus.db")),
        workspace={"root": Path("data/workspaces")},
        artifacts={"root": Path("data/artifacts")},
        session_secret=SecretStr("test-session-secret-that-is-long-enough"),
        bootstrap_admin_password=SecretStr("initial-password"),
    )

    resolved = resolve_runtime_paths(settings, tmp_path)

    assert resolved.database.path == tmp_path / "data/coderus.db"
    assert resolved.workspace.root == tmp_path / "data/workspaces"
    assert resolved.artifacts.root == tmp_path / "data/artifacts"


def test_preview_requires_isolated_runtime_paths(tmp_path: Path) -> None:
    settings = Settings(
        database=DatabaseSettings(path=Path("data/coderus.db")),
        workspace={"root": Path("data/workspaces")},
        artifacts={"root": Path("data/artifacts")},
        session_secret=SecretStr("test-session-secret-that-is-long-enough"),
        bootstrap_admin_password=SecretStr("initial-password"),
    )
    parser = build_parser()
    missing = parser.parse_args(["serve", "--runtime", "preview"])

    with pytest.raises(ValueError, match="preview requires isolated"):
        prepare_runtime_settings(settings, missing, tmp_path)

    args = parser.parse_args(
        [
            "serve",
            "--runtime",
            "preview",
            "--database",
            str(tmp_path / "preview.db"),
            "--workspace",
            str(tmp_path / "workspaces"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--port",
            "18084",
        ]
    )
    prepared = prepare_runtime_settings(settings, args, tmp_path)

    assert prepared.database.path == tmp_path / "preview.db"
    assert prepared.workspace.root == tmp_path / "workspaces"
    assert prepared.artifacts.root == tmp_path / "artifacts"
    assert prepared.server.port == 18084
