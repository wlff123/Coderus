"""平台适配层统一错误模型。

三个基类是消费方应当捕获的稳定契约：

- ``ForgeError``：平台适配层所有错误；
- ``InvalidForgeInput``：输入不合法（URL、分支名、PR 编号等）；
- ``ForgeRemoteError``：远端平台返回错误、响应无效或不可用。

其余具体类型来自 providers 与 publisher 的历史层级，迁移期间保持
原有名称与构造签名不变，全部重定基到上面三个基类。
"""

from __future__ import annotations


class ForgeError(Exception):
    """平台适配层所有错误的基类。"""


class InvalidForgeInput(ForgeError, ValueError):
    """输入不合法：URL、分支名、PR 编号等未通过校验。"""


class ForgeRemoteError(ForgeError):
    """远端平台返回错误、响应无效或不可用。"""


class ProviderError(ForgeError):
    """Issue 平台读取错误的基类。"""


class InvalidProviderUrl(InvalidForgeInput, ProviderError):
    """仓库、Issue 或 PR 地址不合法。"""


class ProviderNotConfiguredError(ProviderError):
    """请求的平台没有配置对应的 Provider。"""


class ProviderRemoteError(ForgeRemoteError, ProviderError):
    """平台读取请求失败或响应无效；携带平台名与限流元数据。"""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after


class PublisherError(ForgeError):
    """PR 发布错误的基类。"""


class UnsupportedPublisher(PublisherError):
    """注册的远端不在支持的平台上。"""


class InvalidPublisherInput(InvalidForgeInput, PublisherError):
    """调用方提供的发布输入不安全或不合法。"""


class RegisteredForkMismatch(PublisherError):
    """鉴权账号的 Fork 与注册的 Fork 不一致。"""


class GitPushError(PublisherError):
    """受控 git push 失败。"""


class PublisherRemoteError(ForgeRemoteError, PublisherError):
    """发布平台返回错误或响应无效。"""


class ForkNotReady(PublisherRemoteError):
    """新建的 Fork 未在时限内就绪。"""
