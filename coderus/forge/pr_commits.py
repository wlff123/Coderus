from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from .errors import PublisherRemoteError

_SHA = re.compile(r"[0-9a-fA-F]{40}")


def pull_request_commit_shas(rows: Iterable[Mapping[str, object]]) -> frozenset[str]:
    shas = set()
    for row in rows:
        sha = row.get("sha")
        if not isinstance(sha, str) or _SHA.fullmatch(sha) is None or sha.lower() in shas:
            raise PublisherRemoteError("平台返回的 PR 提交清单无效或包含重复提交")
        shas.add(sha.lower())
    return frozenset(shas)
