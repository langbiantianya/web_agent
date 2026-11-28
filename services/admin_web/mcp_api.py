"""MCP 服务器的 JSON 形状与错误映射（V4.10）。

页面路由与管理面 JSON API 共用这里的形状与错误翻译，避免「同一个字段在两
个入口报不同的名字」。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from services.admin_web.api_support import ApiError
from services.admin_web.form_errors import _mcp_field_errors

#: 页面与组件权限码。
PAGE_CODE = "mcp"
EDITOR_COMPONENT = "mcp_editor"
REFRESH_COMPONENT = "mcp_refresh"


def server_payload(
    row: Any,
    *,
    tool_count: int = 0,
    used_by: list[str] | None = None,
) -> dict[str, Any]:
    """把 MCP 服务器行整理成前端形状（不含任何凭据值）。

    ``env`` / ``headers`` 里装的就是凭据，所以只回显**键名**，不回显值。
    """
    return {
        "code": row.code,
        "name": row.name,
        "transport": row.transport,
        "command": row.command or "",
        "url": row.url or "",
        "envKeys": sorted((row.env or {}).keys()),
        "headerKeys": sorted((row.headers or {}).keys()),
        "timeoutSec": row.timeout_sec,
        "enabled": bool(row.enabled),
        "toolCount": tool_count,
        "usedBy": list(used_by or []),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def invalid_error(exc: Exception, transport: str) -> ApiError:
    """把校验失败翻译成带字段级错误的 API 错误。"""
    if isinstance(exc, ValidationError):
        errors: dict[str, str] = {}
        for item in exc.errors():
            errors.setdefault(
                str((item.get("loc") or ("form",))[-1]), item.get("msg") or "字段不合法"
            )
        return ApiError(
            "invalid_input", "提交的字段不合法", status_code=400, field_errors=errors
        )
    return ApiError(
        "invalid_input",
        "提交的字段不合法",
        status_code=400,
        field_errors=_mcp_field_errors(exc, transport),
    )