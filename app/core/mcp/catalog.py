"""MCP 工具目录的发现与快照（设计文档 §21.8 第 ① 段）。

连接建立后调 ``tools/list``（**翻完分页**），把每个工具写进 ``mcp_tool`` 快照。

快照是**发现时写入**的，不是实时的：本轮不实现服务端推送
（``notifications/tools/list_changed``），所以工具清单在连接建立时取一次，
之后以快照为准。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mcp.manager import mcp_manager
from app.core.mcp.naming import McpNamingError, build_qualified_name

logger = logging.getLogger(__name__)


class McpCatalogError(Exception):
    """发现失败——连不上或工具目录不可读。

    Attributes:
        reason: 失败类别（``unreachable`` / ``tools_in_use``）。管理端据此
            给出**可区分**的提示，而不是一句「操作未能完成」。
    """

    def __init__(self, message: str, *, reason: str = "unreachable") -> None:
        super().__init__(message)
        self.reason = reason


class CatalogResult:
    """一次目录刷新的结果。"""

    def __init__(self) -> None:
        """初始化计数。"""
        self.imported: list[str] = []
        self.skipped: list[tuple[str, str]] = []

    def as_dict(self) -> dict:
        """转成可序列化的字典（给 API 响应用）。"""
        return {
            "imported": list(self.imported),
            "skipped": [{"tool": name, "reason": reason} for name, reason in self.skipped],
        }


async def refresh_catalog(db: AsyncSession, server) -> CatalogResult:
    """重新发现某台服务器的工具并**整体替换**它的快照。

    Args:
        db: 请求级 AsyncSession。
        server: ORM ``McpServer`` 实例。

    Returns:
        :class:`CatalogResult`：导入了哪些工具、跳过了哪些（为什么）。

    Raises:
        McpCatalogError: 连不上服务器或读取工具目录失败。
    """
    from app.repo.mcp_repo import mcp_tool_repo

    try:
        tools = await mcp_manager.list_tools(server)
    except Exception as exc:  # noqa: BLE001 - 外部边界
        raise McpCatalogError(
            f"连不上 MCP 服务器「{server.code}」或读不到工具目录：{exc}",
            reason="unreachable",
        ) from exc

    allow = set(server.allow_tools or [])
    result = CatalogResult()
    rows: list[dict] = []
    for tool in tools:
        tool_name = getattr(tool, "name", "") or ""
        if allow and tool_name not in allow:
            result.skipped.append((tool_name, "不在该服务器的工具白名单里"))
            continue
        try:
            qualified = build_qualified_name(server.code, tool_name)
        except McpNamingError as exc:
            # 超长或形状不对的工具名**不导入、不截断**——截断会把两个不同的
            # 工具撞成同一个名，那比不导入更糟。
            result.skipped.append((tool_name, str(exc)))
            continue
        annotations = getattr(tool, "annotations", None)
        rows.append(
            {
                "qualified_name": qualified,
                "tool_name": tool_name,
                "description": getattr(tool, "description", None),
                "input_schema": getattr(tool, "input_schema", None),
                "output_schema": getattr(tool, "output_schema", None),
                # 注解是**不可信数据**（§21.10 第 ③ 条）：只存下来给前端展示，
                # 永远不用于权限判定。
                "annotations": annotations.model_dump() if annotations else None,
            }
        )
        result.imported.append(qualified)

    current = await mcp_tool_repo.list_by_server(db, server.id)
    discovered = {row["qualified_name"] for row in rows}
    from app.repo.agent_repo import agent_repo

    bindings = await agent_repo.tool_binding_map(
        db, tenant_id=server.tenant_id
    )
    removed_bound = [
        row.qualified_name
        for row in current
        if row.qualified_name in bindings
        and row.qualified_name not in discovered
    ]
    if removed_bound:
        raise McpCatalogError(
            "以下 MCP 工具仍被 Agent 使用，不能从目录中移除："
            + "、".join(sorted(removed_bound)),
            reason="tools_in_use",
        )

    await mcp_tool_repo.replace_for_server(db, server.id, rows)
    logger.info(
        "MCP %s 目录刷新完成：导入 %s 个，跳过 %s 个",
        server.code,
        len(result.imported),
        len(result.skipped),
    )
    return result
