from __future__ import annotations

from dataclasses import dataclass

from coderus.config import Settings


@dataclass(frozen=True, slots=True)
class CodexAuthStatus:
    mode: str
    ready: bool
    label: str
    detail: str


def inspect_codex_auth(
    settings: Settings,
) -> CodexAuthStatus:
    if settings.model_api_key is None:
        return CodexAuthStatus(
            mode="api_proxy",
            ready=False,
            label="模型 API 代理",
            detail="未配置 CODERUS_MODEL_API_KEY，Agent 执行已阻止",
        )
    return CodexAuthStatus(
        mode="api_proxy",
        ready=True,
        label="模型 API 代理",
        detail="使用任务级短期 Token",
    )
