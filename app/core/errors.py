"""领域异常 —— 与 HTTP 层解耦，路由层负责映射状态码。"""

from __future__ import annotations


class AppError(Exception):
    """业务异常基类。"""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, data: dict | None = None) -> None:
        """构造业务异常。

        Args:
            message: 面向用户的中文错误描述，路由层会原样放进响应体的
                ``message`` 字段——不要把堆栈、SQL 或内部 id 塞进来。
            data: 可选的补充结构（如出错字段），序列化成响应体的 ``data``。

        子类只覆盖类属性 ``status_code`` / ``code`` 就能改变 HTTP 语义，异常
        本身不感知 HTTP（见模块 docstring）。
        """
        super().__init__(message)
        self.message = message
        self.data = data


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """该会话已有进行中的 run。"""

    status_code = 409
    code = "conflict"


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(AppError):
    """ticket 无效或过期。"""

    status_code = 401
    code = "unauthorized"


class GoneError(AppError):
    """事件已归档清理，无法回放。"""

    status_code = 410
    code = "gone"


class RunCancelled(Exception):
    """内部信号：用户主动取消生成。"""
