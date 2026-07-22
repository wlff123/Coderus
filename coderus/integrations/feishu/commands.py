from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CommandKind = Literal["help", "status", "tasks", "task", "dispatch", "review", "unknown"]
TASK_PATTERN = re.compile(r"^RE-(\d+)$", re.IGNORECASE)
ISSUE_URL_PATTERN = re.compile(r"^https://[^\s]+/issues/\d+$")
PULL_REQUEST_URL_PATTERN = re.compile(
    r"^https://[^\s]+/(?:pulls?|merge_requests)/\d+$"
)
MARKDOWN_LINK_PATTERN = re.compile(r"^\[(https://[^\]\s]+)\]\((https://[^\s)]+)\)$")
INTRO_QUESTIONS = frozenset(
    {
        "你会干什么",
        "你会做什么",
        "你能干什么",
        "你能做什么",
        "你会什么",
        "你是谁",
        "介绍一下",
        "自我介绍",
        "有什么功能",
        "有哪些功能",
        "支持什么命令",
        "支持哪些命令",
        "怎么用",
        "如何使用",
    }
)


@dataclass(frozen=True)
class BotCommand:
    kind: CommandKind
    argument: str | None = None


@dataclass(frozen=True)
class IncomingFeishuMessage:
    message_id: str
    event_id: str | None
    chat_id: str
    chat_type: str
    sender_open_id: str | None
    text: str
    mentioned_bot: bool


def parse_command(text: str) -> BotCommand:
    value = " ".join(text.strip().split())
    normalized_question = value.rstrip("?!？！。.")
    if value.casefold() in {"帮助", "help"} or normalized_question in INTRO_QUESTIONS:
        return BotCommand("help")
    if value == "状态":
        return BotCommand("status")
    if value == "任务":
        return BotCommand("tasks")

    argument = _command_argument(value, "任务")
    if argument is not None:
        match = TASK_PATTERN.fullmatch(argument)
        if match:
            return BotCommand("task", f"RE-{int(match.group(1))}")
    argument = _command_argument(value, "派发")
    if argument is not None and ISSUE_URL_PATTERN.fullmatch(argument):
        return BotCommand("dispatch", argument)
    argument = _command_argument(value, "检视")
    if argument is not None and PULL_REQUEST_URL_PATTERN.fullmatch(argument):
        return BotCommand("review", argument)
    return BotCommand("unknown")


def _command_argument(value: str, command: str) -> str | None:
    if not value.startswith(command):
        return None
    argument = value[len(command) :].strip()
    markdown_link = MARKDOWN_LINK_PATTERN.fullmatch(argument)
    if markdown_link is None:
        return argument
    label, target = markdown_link.groups()
    return target if label == target else argument
