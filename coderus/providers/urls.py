"""迁移期转发：URL 解析已统一到 coderus.forge.urls。"""

from coderus.forge.urls import (
    parse_issue_url,
    parse_pull_request_url,
    parse_repository_url,
)

__all__ = ["parse_issue_url", "parse_pull_request_url", "parse_repository_url"]
