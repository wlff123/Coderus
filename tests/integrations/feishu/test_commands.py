import pytest

from coderus.integrations.feishu.commands import BotCommand, parse_command


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮助", BotCommand("help")),
        ("help", BotCommand("help")),
        ("你会干什么", BotCommand("help")),
        ("你能做什么？", BotCommand("help")),
        ("你是谁", BotCommand("help")),
        ("介绍一下", BotCommand("help")),
        ("自我介绍", BotCommand("help")),
        ("有什么功能", BotCommand("help")),
        ("支持哪些命令", BotCommand("help")),
        ("怎么用", BotCommand("help")),
        ("状态", BotCommand("status")),
        ("任务", BotCommand("tasks")),
        ("任务 RE-5", BotCommand("task", "RE-5")),
        ("任务 re-5", BotCommand("task", "RE-5")),
        (
            "派发 https://github.com/volcengine/OpenViking/issues/1487",
            BotCommand(
                "dispatch",
                "https://github.com/volcengine/OpenViking/issues/1487",
            ),
        ),
        (
            "检视 https://github.com/acme/widgets/pull/17",
            BotCommand("review", "https://github.com/acme/widgets/pull/17"),
        ),
        (
            "检视 https://gitcode.com/acme/widgets/merge_requests/17",
            BotCommand(
                "review",
                "https://gitcode.com/acme/widgets/merge_requests/17",
            ),
        ),
        (
            "检视https://gitcode.com/acme/widgets/pull/17",
            BotCommand("review", "https://gitcode.com/acme/widgets/pull/17"),
        ),
        (
            "检视[https://gitcode.com/acme/widgets/pull/17]"
            "(https://gitcode.com/acme/widgets/pull/17)",
            BotCommand("review", "https://gitcode.com/acme/widgets/pull/17"),
        ),
        (
            "派发https://github.com/volcengine/OpenViking/issues/1487",
            BotCommand(
                "dispatch",
                "https://github.com/volcengine/OpenViking/issues/1487",
            ),
        ),
        ("任务RE-5", BotCommand("task", "RE-5")),
    ],
)
def test_parse_supported_commands(text: str, expected: BotCommand) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "帮我看看状态",
        "派发",
        "派发 OpenViking#1487",
        "任务 5",
        "取消 RE-5",
    ],
)
def test_parse_unknown_or_incomplete_commands(text: str) -> None:
    assert parse_command(text) == BotCommand("unknown")


def test_dispatch_rejects_trailing_instructions() -> None:
    text = "派发 https://github.com/octo/demo/issues/1 顺便重构"

    assert parse_command(text) == BotCommand("unknown")


def test_review_rejects_trailing_instructions() -> None:
    text = "检视 https://github.com/octo/demo/pull/1 顺便重构"

    assert parse_command(text) == BotCommand("unknown")
