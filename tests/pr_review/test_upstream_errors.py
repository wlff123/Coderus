import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from coderus.pr_review.upstream_errors import upstream_error_message


def extract(event, **kwargs):
    result = SimpleNamespace(stdout=json.dumps(event), stderr="", output_truncated=False)
    return upstream_error_message(result, proxy_token="proxy-test", workspace=Path.cwd(), **kwargs)


def test_high_demand_and_secret_redaction():
    message = extract({"type": "turn.failed", "error": {"message":
        "High demand opaque-client-key proxy-test sk-test123 "
        "https://example.test/v1?key=hidden api_key='hidden-key'"}},
        known_secrets=("opaque-client-key",))
    assert "High demand" in message
    for value in ("opaque-client-key", "proxy-test", "sk-test123", "hidden", "example.test"):
        assert value not in message


@pytest.mark.parametrize("event", [
    {"type": "item.completed", "item": {"type": "agent_message", "text": "503 upstream"}},
    {"type": "item.completed", "item": {"type": "command_execution", "output": "503"}},
    {"type": "error", "message": "workspace does not exist"},
    {"type": "turn.failed", "error": None},
    [],
])
def test_non_transport_output_is_not_forwarded(event):
    assert extract(event) is None
