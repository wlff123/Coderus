from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, HttpUrl, SecretStr, model_validator


class ServerSettings(BaseModel):
    mode: Literal["local", "public"] = "local"
    bind: str = "127.0.0.1"
    port: int = Field(default=18082, ge=1, le=65535)
    public_url: HttpUrl | None = None

    @model_validator(mode="after")
    def validate_exposure(self) -> ServerSettings:
        if self.bind not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Manager must bind to a loopback address")
        if self.mode == "local" and self.public_url is not None:
            raise ValueError("public_url must be empty in local mode")
        if self.mode == "public":
            if self.public_url is None or self.public_url.scheme != "https":
                raise ValueError("public mode requires an HTTPS public_url")
        return self


class DatabaseSettings(BaseModel):
    path: Path = Path("data/coderus.db")


class WorkspaceSettings(BaseModel):
    root: Path = Path("data/workspaces")


class RunnerSettings(BaseModel):
    network_access: bool = True


class ArtifactsSettings(BaseModel):
    root: Path = Path("data/artifacts")


class SchedulerSettings(BaseModel):
    issue_poll_seconds: int = Field(default=300, ge=30)
    global_task_limit: int = Field(default=8, ge=1)
    per_user_task_limit: int = Field(default=2, ge=1)
    max_agent_processes: int = Field(default=16, ge=1)


class CodexSettings(BaseModel):
    binary: str = "codex"
    model: str | None = None
    stage_timeout_seconds: int = Field(default=3600, ge=60)
    base_url: str | None = None
    proxy_port: int = Field(default=18083, ge=1, le=65535)
    sandbox_mode: Literal["workspace-write", "danger-full-access"] = "workspace-write"
    auth_mode: Literal["api_proxy"] = "api_proxy"


class AssistantSettings(BaseModel):
    enabled: bool = True


class GitSettings(BaseModel):
    user_name: str = "Coderus Bot"
    user_email: str = "coderus@example.com"


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    runner: RunnerSettings = Field(default_factory=RunnerSettings)
    artifacts: ArtifactsSettings = Field(default_factory=ArtifactsSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    assistant: AssistantSettings = Field(default_factory=AssistantSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    session_secret: SecretStr = Field(
        validation_alias=AliasChoices("session_secret", "CODERUS_SESSION_SECRET"),
        min_length=24,
    )
    bootstrap_admin_password: SecretStr = Field(
        validation_alias=AliasChoices(
            "bootstrap_admin_password", "CODERUS_BOOTSTRAP_ADMIN_PASSWORD"
        ),
        min_length=8,
    )
    credential_encryption_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "credential_encryption_key",
            "CODERUS_CREDENTIAL_ENCRYPTION_KEY",
        ),
    )
    model_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("model_api_key", "CODERUS_MODEL_API_KEY"),
    )
    github_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("github_token", "CODERUS_GITHUB_TOKEN"),
    )
    gitcode_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("gitcode_token", "CODERUS_GITCODE_TOKEN"),
    )

    @model_validator(mode="after")
    def validate_cross_fields(self) -> Settings:
        if self.scheduler.per_user_task_limit > self.scheduler.global_task_limit:
            raise ValueError("per-user task limit cannot exceed global task limit")
        if self.model_api_key is not None and not (self.codex.base_url and self.codex.model):
            raise ValueError("model API key requires codex.base_url and codex.model")
        if self.codex.base_url and self.model_api_key is None:
            raise ValueError("codex.base_url requires a model API key")
        if self.codex.proxy_port == self.server.port:
            raise ValueError("model proxy port must differ from the Manager port")
        return self


def load_settings(path: Path, environ: Mapping[str, str] | None = None) -> Settings:
    source = environ if environ is not None else __import__("os").environ
    if not path.exists():
        raise FileNotFoundError(f"configuration file does not exist: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    data = dict(raw)
    if "CODERUS_MODEL_BASE_URL" in source:
        data.setdefault("codex", {})["base_url"] = source["CODERUS_MODEL_BASE_URL"]
    env_keys = (
        "CODERUS_SESSION_SECRET",
        "CODERUS_BOOTSTRAP_ADMIN_PASSWORD",
        "CODERUS_CREDENTIAL_ENCRYPTION_KEY",
        "CODERUS_MODEL_API_KEY",
        "CODERUS_GITHUB_TOKEN",
        "CODERUS_GITCODE_TOKEN",
    )
    data.update({key: source[key] for key in env_keys if key in source})
    return Settings.model_validate(data)
