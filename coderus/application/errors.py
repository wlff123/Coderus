"""应用服务层错误：入口据此映射 HTTP 状态码或飞书文案。"""

from __future__ import annotations


class CommandError(Exception):
    """所有应用服务错误的基类。"""


class NotFound(CommandError):
    """目标对象不存在。"""


class Forbidden(CommandError):
    """执行者没有操作该对象的权限。"""


class Conflict(CommandError):
    """对象当前状态不允许该操作，消息为用户可见文案。"""
