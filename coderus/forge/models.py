"""平台适配层共享数据模型：平台名与仓库、Issue 快照。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ProviderName = Literal["github", "gitcode"]


@dataclass(frozen=True, slots=True)
class Repository:
    provider: ProviderName
    owner: str
    name: str
    canonical_url: str
    default_branch: str | None = None
    is_private: bool | None = None
    issues_enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class Issue:
    repository: Repository
    external_id: str
    number: int
    title: str
    body: str | None
    state: Literal["open", "closed"]
    labels: tuple[str, ...]
    canonical_url: str
    created_at: datetime | None
    updated_at: datetime | None
