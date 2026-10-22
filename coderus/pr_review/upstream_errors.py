"""Extract model transport errors, never repository/tool output, for safe replies."""

from __future__ import annotations

import json
import re
from pathlib import Path

_UPSTREAM = re.compile(
    r"\b[45]\d\d\b|high demand|rate.?limit|too many requests|overload|"
    r"quota|invalid.api.key|incorrect.api.key|authentication|unauthorized|"
    r"service unavailable|bad gateway|model.*(?:not found|does not exist)|"
    r"context.length|context window|invalid.request|failed to (?:send|connect)|"
    r"error sending request|stream disconnect|connection (?:reset|refused)|"
    r"request timed out|server error|upstream",
    re.IGNORECASE,
)


def redact_upstream_message(message: str, secrets: set[str]) -> str:
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"(?:https?|wss?)://[^\s\"<>]+", "[URL REDACTED]", message)
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", message)
    message = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(
        r"(?i)(?:api[ _-]?key(?: provided)?|access[ _-]?token|token|secret|authorization)"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        "credential=[REDACTED]",
        message,
    )
    return " ".join(message.split())[:280]


def upstream_error_message(
    result, *, proxy_token: str | None, workspace: Path, known_secrets: tuple[str, ...] = (),
) -> str | None:
    if result.output_truncated:
        return None
    candidates: list[str] = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            message = event.get("message")
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        else:
            continue
        if isinstance(message, str) and _UPSTREAM.search(message):
            candidates.append(message)
    if not candidates:
        # Older CLI versions report transport failures on stderr rather than JSON.
        for line in result.stderr.splitlines():
            if re.search(r"(?i)model call failed|unexpected status|error.*\b[45]\d\d\b", line):
                if _UPSTREAM.search(line):
                    candidates.append(line)
    if not candidates:
        return None
    absolute = str(workspace.resolve())
    return redact_upstream_message(candidates[-1], {
        *known_secrets, proxy_token or "", absolute, absolute.replace("\\", "/"),
        absolute.replace("/", "\\"),
    })
