"""Agent 表单里「LLM 连接」相关的字段与展示。

这块单独成模块有两个原因：

1. **行数上限**：``agents.py`` 还要放列表与面板渲染，塞不下这些细节；
2. **边界清晰**：连接下拉的候选、存量 Agent 的旧地址脱敏展示，都是
   「连接」这一个概念的事，与列表布局无关。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import div, small, span

from services.admin_web.views.fields import Field


def _connection_options(
    endpoints: Iterable[Any],
) -> tuple[tuple[str, str], ...]:
    """构造 Agent 当前可选择的 LLM 连接下拉候选。"""
    rows = [endpoint for endpoint in endpoints if endpoint.enabled]
    if not rows:
        return (("No enabled LLM connections configured", ""),)
    options = [("Select a configured LLM connection", "")]
    for endpoint in rows:
        model = endpoint.model or "no default model"
        options.append(
            (
                f"{endpoint.name or endpoint.code} ({endpoint.code}) · {model}",
                endpoint.code,
            )
        )
    return tuple(options)


def _masked_base_url(raw: object) -> str:
    """脱敏展示存量 Agent 的旧地址（UX-08）。

    只保留**协议 + 主机**（可带端口）：路径常常带租户标识或令牌，
    查询串更可能直接是密钥。主机名是排查「它到底连到哪儿了」必需的
    那一部分，剩下的不该出现在页面上。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    without_scheme = text.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0]
    if not host:
        return ""
    scheme = text.split("://", 1)[0] if "://" in text else "https"
    # 主机里可能带 ``user:pass@``——凭据一律去掉。
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    return f"{scheme}://{host}"


def _connection_cell(
    row: Any, endpoints_by_code: dict[str, Any]
) -> Any:
    """渲染 Agent 使用的 LLM 连接。

    未绑定连接的**存量** Agent 额外显示脱敏后的旧地址与迁移指引
    （UX-08）：只写一句「Legacy / manual」的话，管理员既不知道它到底
    连到哪儿，也不知道该怎么迁。
    """
    linked_code = getattr(row, "llm_endpoint_code", None)
    endpoint = endpoints_by_code.get(linked_code) if linked_code else None
    if linked_code and endpoint is not None:
        return div(
            span(endpoint.name or endpoint.code),
            small(
                linked_code + ("" if endpoint.enabled else " · disabled"),
                class_="block-muted",
            ),
        )
    if linked_code:
        return div(
            span("Missing LLM connection"),
            small(linked_code, class_="block-muted"),
        )
    legacy_url = _masked_base_url(getattr(row, "base_url", ""))
    return div(
        span("Legacy / manual"),
        small(
            legacy_url or "No LLM connection selected",
            class_="block-muted",
        ),
        *(
            [
                small(
                    "Migrate by picking an LLM connection in the editor.",
                    class_="warning-text",
                )
            ]
            if legacy_url
            else []
        ),
    )


def agent_fields(
    endpoints: Iterable[Any] = (),
) -> tuple[Field, ...]:
    """Agent 创建与编辑共用字段；连接列表由请求时数据库决定。"""
    return (
        Field("name", "Name", required=True),
        Field(
            "llm_endpoint_code",
            "LLM connection",
            kind="select",
            options=_connection_options(endpoints),
            required=True,
        ),
        Field(
            "model",
            "Model",
            placeholder="Uses the LLM connection default when blank",
        ),
        Field(
            "system_prompt",
            "System prompt",
            kind="textarea",
            required=True,
            rows=8,
            edit_rows=10,
        ),
    )


def agent_create_fields(
    endpoints: Iterable[Any] = (),
) -> tuple[Field, ...]:
    """创建表单额外包含稳定编码与初始启用状态。"""
    return (
        Field("code", "Code", required=True, modes=("create",)),
        *agent_fields(endpoints),
        Field(
            "enabled",
            "Enabled",
            kind="select",
            modes=("create",),
            options=(("Yes", "1"), ("No", "0")),
        ),
    )


AGENT_FIELDS = agent_fields()
AGENT_CREATE_FIELDS = agent_create_fields()


__all__ = [
    "AGENT_CREATE_FIELDS",
    "AGENT_FIELDS",
    "agent_create_fields",
    "agent_fields",
]
