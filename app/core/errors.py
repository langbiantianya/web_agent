"""领域异常 —— 与 HTTP 层解耦，路由层负责映射状态码。"""

from __future__ import annotations

import re

# 错误信息脱敏用的模式。**只认形态、不认具体厂商**——异常文本来自各种第三方
# SDK，字段名与格式各不相同，与其逐个适配，不如按「像密钥的东西」来遮。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # sk- 开头的 API key（OpenAI / MiniMax 等）
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    # Authorization / Bearer 头
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{6,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(authorization)(\"?\s*[:=]\s*\"?)[^\s\"',]{6,}"), r"\1\2***"),
    # api_key / token / secret 之类的赋值
    (
        re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\"',]{4,}"),
        r"\1\2***",
    ),
    # URL 里带的用户名密码
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), "***:***"),
)


def redact_error_message(text: str | None) -> str | None:
    """把错误信息里的密钥形态内容遮掉。

    ## 为什么需要

    节点的失败原因会落进 ``run.error_msg`` / ``run_step.error_msg``，而
    ``GET /api/runs/{id}`` 会把它原样回显。LLM SDK 的异常文本里常带着请求 URL、
    打码后的 key、甚至完整的 Authorization 头——那些一旦进入一个可被读取的
    响应体，就等于把凭证抄送了出去。

    这里**只做遮蔽、不做删减**：错误信息本身是排障的关键（"connection refused"
    和 "invalid api key" 是两回事），所以保留原文结构、只把像密钥的部分换成
    ``***``。

    Args:
        text: 原始错误信息。

    Returns:
        脱敏后的文本；``None`` 原样返回。
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


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


class SensitiveContentError(AppError):
    """提示词被敏感词前置过滤拦下。

    刻意用 **422** 而不是 400：400 在这套约定里表示「请求参数/请求体不合法」
    （格式错误、字段缺失），而 422 表示「格式没问题，但内容本身不被接受」。
    前端可以据此区分「你参数写错了」和「你这句话我们不接」。

    ``data`` 里带上命中的片段，让用户知道大概哪里需要改写——过滤器本身
    不算严，用户改一两个字就能过的体验，远好过一句「内容违规」。
    """

    status_code = 422
    code = "sensitive_content"


class RunCancelled(Exception):
    """内部信号：用户主动取消生成。"""


class ToolLoopExceeded(Exception):
    """工具调用轮数超限——模型在工具上陷住了。

    刻意**不重试**：重试同一个节点，模型大概率还是同样地陷住，白烧一轮 token。
    直接失败并把「超过几轮」告诉用户，比让它继续烧钱有用。
    """

    def __init__(self, max_rounds: int) -> None:
        """构造异常。

        Args:
            max_rounds: 触发本异常时允许的最大轮数。
        """
        super().__init__(f"工具调用超过 {max_rounds} 轮，模型在工具上陷住了")
        self.max_rounds = max_rounds
        self.message = str(self)
