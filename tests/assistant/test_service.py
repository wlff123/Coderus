from __future__ import annotations

import json

import httpx
from sqlalchemy.orm import Session

from coderus.assistant.service import UNAVAILABLE_TEXT, ModelAssistant


def reply_payload(answer: str) -> dict:
    return {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": answer}]}
        ]
    }


def make_assistant(handler, *, base_url: str = "https://model.example/v1") -> ModelAssistant:
    return ModelAssistant(
        base_url=base_url,
        api_key="secret-key",
        model="gpt-test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_question_returns_model_answer(session: Session) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=reply_payload("用依赖注入解耦"))

    answer = make_assistant(handler).answer("如何解耦模块？", session)

    assert answer == "用依赖注入解耦"
    assert seen["url"] == "https://model.example/v1/responses"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "gpt-test"
    assert "如何解耦模块？" in seen["body"]["input"]
    assert "任务统计：" in seen["body"]["input"]


def test_base_url_without_v1_suffix(session: Session) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=reply_payload("ok"))

    make_assistant(handler, base_url="https://model.example").answer("问题", session)

    assert seen["url"] == "https://model.example/v1/responses"


def test_long_answer_is_truncated(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply_payload("长" * 3000))

    assert make_assistant(handler).answer("问题", session) == "长" * 2000


def test_http_error_degrades_gracefully(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    assert make_assistant(handler).answer("问题", session) == UNAVAILABLE_TEXT


def test_empty_output_degrades_gracefully(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": []})

    assert make_assistant(handler).answer("问题", session) == UNAVAILABLE_TEXT


def test_blank_answer_degrades_gracefully(session: Session) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=reply_payload("   "))

    assert make_assistant(handler).answer("问题", session) == UNAVAILABLE_TEXT
