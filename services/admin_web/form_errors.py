"""管理端表单校验错误到字段级文案的映射（UX-03）。

校验失败时页面要**指出具体字段**，而不是丢一句通用提示。这里把
pydantic 的 :class:`ValidationError` 摊平成 ``{字段名: 文案}``，并为
业务级异常（连接方式、整数解析等）指定落点字段。
"""

from __future__ import annotations

from pydantic import ValidationError

from services.admin_web.form_state import field_errors

def _llm_field_errors(exc: Exception) -> dict[str, str]:
    """把 LLM 表单的校验错误映射到字段（UX-03）。

    pydantic 的 :class:`ValidationError` 能给出字段名；``ValueError``
    来自 ``priority`` 的整数解析（表单里那一栏填了非数字），定位到
    ``priority`` 字段上。
    """
    if isinstance(exc, ValidationError):
        return field_errors(exc)
    return {"priority": "Priority must be a whole number."}

def _mcp_field_errors(
    exc: Exception, transport: str | None = None
) -> dict[str, str]:
    """把 MCP 表单的校验错误映射到字段（UX-03）。

    pydantic 的 :class:`ValidationError` 给出字段名；``ValueError`` 与
    :class:`McpTransportError` 是连接层面的问题，落在 ``url`` / ``command``
    两个字段之一——传输方式决定了哪个字段才是有效输入。

    Args:
        exc: 捕获到的异常。
        transport: 表单提交的传输方式（``stdio`` / ``http``）。连接失败时
            要把错误标在**真正生效的那个输入框**上：stdio 走 ``command``，
            http 走 ``url``。否则 stdio 注册失败会把错误落到被禁用隐藏的
            ``url`` 栏上，用户根本看不到错在哪（UX-33）。

    Returns:
        字段名到文案的映射。
    """
    if isinstance(exc, ValidationError):
        errors = field_errors(exc)
        if errors:
            return errors
    code = str(exc)
    if code in {"url_required", "url_not_allowed_for_http"}:
        return {"url": "Enter the HTTP URL for this server."}
    if code in {"command_required", "command_not_allowed_for_stdio"}:
        return {"command": "Enter the stdio command for this server."}
    if code == "timeout_sec" or (
        isinstance(exc, ValueError) and "invalid literal for int" in code
    ):
        return {"timeout_sec": "Timeout must be a whole number."}
    if transport == "stdio":
        return {"command": "The stdio command could not be started."}
    if transport == "http":
        return {"url": "The HTTP URL could not be validated."}
    return {"url": "The connection could not be validated."}


def _oauth_field_errors(exc: Exception) -> dict[str, str]:
    """把校验错误映射成字段级文案（UX-03）。"""
    if isinstance(exc, ValidationError):
        return field_errors(exc)
    return {"display_name": "The submitted values are invalid."}

