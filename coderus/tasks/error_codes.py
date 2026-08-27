"""稳定任务错误代码：页面与飞书按代码渲染文案，恢复动作不依赖异常字符串。

十个类别与演进设计 7.2 节一一对应。既有的特化代码保持不变，视作对应
类别的细分：``developer_report_invalid``（agent_output_invalid）、
``manager_restarted``、``worker_interrupted``（executor_interrupted）、
``publish_existing``、``pr_feedback_revision``（恢复标记，非失败）。

异常类型可以通过类属性 ``error_code`` 自行声明类别；未声明时按
``classify_exception`` 的内置映射归类。
"""

from __future__ import annotations

from enum import StrEnum

from coderus.forge.errors import ForgeRemoteError, GitPushError, InvalidForgeInput
from coderus.forge.registry import ForgeNotConfigured
from coderus.processes import CommandOutputLimitExceeded, CommandResourceLimitExceeded

_AUTH_STATUS_CODES = frozenset({401, 403})


class TaskErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    FORGE_AUTH_FAILED = "forge_auth_failed"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    REPOSITORY_BUILD_FAILED = "repository_build_failed"
    AGENT_OUTPUT_INVALID = "agent_output_invalid"
    TESTS_FAILED = "tests_failed"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    EXECUTOR_INTERRUPTED = "executor_interrupted"
    SIDE_EFFECT_UNKNOWN = "side_effect_unknown"
    INTERNAL_ERROR = "internal_error"


def classify_exception(exc: BaseException) -> TaskErrorCode:
    declared = getattr(exc, "error_code", None)
    if isinstance(declared, TaskErrorCode):
        return declared
    if isinstance(exc, ForgeNotConfigured):
        return TaskErrorCode.FORGE_AUTH_FAILED
    if isinstance(exc, InvalidForgeInput):
        return TaskErrorCode.INVALID_INPUT
    if isinstance(exc, ForgeRemoteError):
        if getattr(exc, "status_code", None) in _AUTH_STATUS_CODES:
            return TaskErrorCode.FORGE_AUTH_FAILED
        return TaskErrorCode.UPSTREAM_UNAVAILABLE
    if isinstance(exc, GitPushError):
        return TaskErrorCode.UPSTREAM_UNAVAILABLE
    if isinstance(exc, CommandResourceLimitExceeded | CommandOutputLimitExceeded):
        return TaskErrorCode.RESOURCE_LIMIT_EXCEEDED
    if isinstance(exc, TimeoutError):
        # 编排器只对外部写操作（如发布 PR 评论）设置整体超时，
        # 超时说明远端可能已执行，结果不确定。
        return TaskErrorCode.SIDE_EFFECT_UNKNOWN
    if isinstance(exc, ConnectionError):
        return TaskErrorCode.UPSTREAM_UNAVAILABLE
    if isinstance(exc, ValueError):
        return TaskErrorCode.INVALID_INPUT
    return TaskErrorCode.INTERNAL_ERROR
