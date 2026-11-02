"""MCP 服务器管理路由（设计文档 §21.11）。

**全部是管理员专属**（§21.10 第 ① 条）：注册一台 MCP 服务器不是普通配置操作，
而是**部署级操作**——stdio 那条路里，配置里的 ``command`` / ``args`` 直接决定
执行什么程序。内置工具的配置是「名字」，MCP stdio 的配置是「程序」；前者是
数据，后者是代码，所以后者的权限必须更高一个级别。

只读的两条（列服务器、看已发现的工具）也让管理员走——它们没有写入能力，
但也不需要为「看看有哪些服务器」单独开一套鉴权。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AdminDep, DbSession
from app.core.crypto import encrypt
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.manager import mcp_manager
from app.core.mcp.transport import McpTransportError, check_command_allowed, check_url_allowed
from app.models.mcp_server import TRANSPORT_HTTP, TRANSPORT_STDIO
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.schemas.mcp import (
    McpRefreshOut,
    McpServerCreate,
    McpServerOut,
    McpServerUpdate,
    McpToolOut,
)

router = APIRouter(prefix="/api/mcp/servers", tags=["mcp"])


def _to_out(server, tool_count: int = 0) -> McpServerOut:
    """ORM ``McpServer`` → 对外响应模型。

    ``env`` / ``headers`` **只回显键名**：值是凭据，明文一律不进响应体。

    Args:
        server: ORM ``McpServer`` 实例。
        tool_count: 已发现的工具数。

    Returns:
        ``McpServerOut``。
    """
    return McpServerOut(
        id=server.id,
        code=server.code,
        name=server.name or server.code,
        transport=server.transport,
        command=server.command,
        args=list(server.args or []) or None,
        env_keys=sorted((server.env or {}).keys()),
        url=server.url,
        header_keys=sorted((server.headers or {}).keys()),
        allow_tools=list(server.allow_tools or []) or None,
        timeout_sec=server.timeout_sec or 30,
        enabled=bool(server.enabled),
        version=server.version or 1,
        online=mcp_manager.is_available(server.code),
        unavailable_reason=mcp_manager.unavailable_reason(server.code),
        tool_count=tool_count,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _encrypt_map(values: dict[str, str] | None) -> dict[str, str] | None:
    """把 ``{键: 明文}`` 变成 ``{键: 密文}``。

    Args:
        values: 明文键值对。

    Returns:
        密文键值对；入参为 None 时返回 None。
    """
    if values is None:
        return None
    return {key: encrypt(value) for key, value in values.items()}


def _check_transport_fields(transport: str, fields: dict) -> None:
    """校验「传输方式」与「字段」对得上。

    Args:
        transport: ``stdio`` 或 ``http``。
        fields: 待校验的字段字典。

    Raises:
        BadRequestError: 字段与传输方式不匹配。
    """
    if transport == TRANSPORT_STDIO:
        if not fields.get("command"):
            raise BadRequestError("stdio 传输必须提供 command")
        if fields.get("url"):
            raise BadRequestError("stdio 传输不接受 url")
    elif transport == TRANSPORT_HTTP:
        if not fields.get("url"):
            raise BadRequestError("http 传输必须提供 url")
        if fields.get("command"):
            raise BadRequestError("http 传输不接受 command")


@router.get("", response_model=list[McpServerOut])
async def list_servers(db: DbSession, admin: AdminDep) -> list[McpServerOut]:
    """列出全部 MCP 服务器 + 在线状态 + 工具数。"""
    servers = await mcp_server_repo.list(db, limit=500)
    out: list[McpServerOut] = []
    for server in servers:
        tools = await mcp_tool_repo.list_by_server(db, server.id)
        out.append(_to_out(server, len(tools)))
    return out


@router.post("", response_model=McpServerOut, status_code=201)
async def create_server(
    payload: McpServerCreate, db: DbSession, admin: AdminDep
) -> McpServerOut:
    """注册一台 MCP 服务器。

    **这是部署级操作**：stdio 的 ``command`` 决定执行什么程序，所以命令路径
    必须在 ``MCP_ALLOWED_COMMAND_PREFIXES`` 白名单内（默认空 = 禁止）。
    """
    if await mcp_server_repo.get_by_code(db, payload.code):
        raise ConflictError(f"MCP 服务器 code 已存在：{payload.code}")
    fields = {
        "command": payload.command,
        "args": payload.args,
        "url": payload.url,
    }
    _check_transport_fields(payload.transport, fields)
    if payload.transport == TRANSPORT_STDIO:
        # 命令白名单在**保存期**就查——不查的话配置能存进去，等到第一次调用
        # 才失败，而那时候模型已经拿着这个工具名在跑了。
        try:
            check_command_allowed(payload.command or "")
        except McpTransportError as exc:
            raise BadRequestError(str(exc)) from exc
    else:
        try:
            # 保存期用 ``require_dns=False``：DNS 解析失败只说明「此刻查不到这个
            # 主机」，不是「这个地址危险」。真正连上去之前会再查一次，那时不容情。
            await check_url_allowed(payload.url or "", require_dns=False)
        except McpTransportError as exc:
            raise BadRequestError(str(exc)) from exc

    server = await mcp_server_repo.create(
        db,
        code=payload.code,
        name=payload.name or payload.code,
        transport=payload.transport,
        command=payload.command,
        args=payload.args,
        env=_encrypt_map(payload.env),
        url=payload.url,
        headers=_encrypt_map(payload.headers),
        allow_tools=payload.allow_tools,
        timeout_sec=payload.timeout_sec,
        enabled=1 if payload.enabled else 0,
    )
    return _to_out(server, 0)


@router.get("/{code}", response_model=McpServerOut)
async def get_server(code: str, db: DbSession, admin: AdminDep) -> McpServerOut:
    """查一台服务器。"""
    server = await mcp_server_repo.get_by_code(db, code)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    tools = await mcp_tool_repo.list_by_server(db, server.id)
    return _to_out(server, len(tools))


@router.put("/{code}", response_model=McpServerOut)
async def update_server(
    code: str, payload: McpServerUpdate, db: DbSession, admin: AdminDep
) -> McpServerOut:
    """更新服务器：``version+1``，并**失效连接与快照**。

    连接与快照都要失效：改了命令或端点之后，旧连接连的是旧程序，旧快照描述
    的是旧工具——留着它们就是「配置说 A、实际跑 B」。
    """
    server = await mcp_server_repo.get_by_code(db, code)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    fields = payload.model_dump(exclude_unset=True)
    if "env" in fields:
        fields["env"] = _encrypt_map(fields["env"])
    if "headers" in fields:
        fields["headers"] = _encrypt_map(fields["headers"])
    if "command" in fields and fields["command"]:
        try:
            check_command_allowed(fields["command"])
        except McpTransportError as exc:
            raise BadRequestError(str(exc)) from exc
    if "url" in fields and fields["url"]:
        try:
            await check_url_allowed(fields["url"], require_dns=False)
        except McpTransportError as exc:
            raise BadRequestError(str(exc)) from exc
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    updated = await mcp_server_repo.update(db, code, fields, bump_version=True)
    if updated is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    # 连接与快照失效：配置变了，旧的东西就不能继续用了。
    await mcp_manager.close_all()
    await mcp_tool_repo.replace_for_server(db, updated.id, [])
    return _to_out(updated, 0)


@router.delete("/{code}", status_code=204)
async def delete_server(code: str, db: DbSession, admin: AdminDep) -> None:
    """删除服务器（连带删掉它的工具快照）。"""
    ok = await mcp_server_repo.delete(db, code)
    if not ok:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    await mcp_manager.close_all()


@router.post("/{code}/refresh", response_model=McpRefreshOut)
async def refresh_server(code: str, db: DbSession, admin: AdminDep) -> McpRefreshOut:
    """重连并刷新工具快照。

    这是「MCP 服务器从来没连过 → 保存期校验直接 400」那条提示的解法：先刷新
    一次目录，工具进快照了，Agent 才能绑。
    """
    server = await mcp_server_repo.get_by_code(db, code)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    try:
        result = await refresh_catalog(db, server)
    except McpCatalogError as exc:
        raise BadRequestError(str(exc)) from exc
    return McpRefreshOut(
        server=code, imported=result.imported, skipped=result.as_dict()["skipped"]
    )


@router.get("/{code}/tools", response_model=list[McpToolOut])
async def list_server_tools(code: str, db: DbSession, admin: AdminDep) -> list[McpToolOut]:
    """该服务器**已发现**的工具（快照，不是实时）。"""
    server = await mcp_server_repo.get_by_code(db, code)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    rows = await mcp_tool_repo.list_by_server(db, server.id)
    return [
        McpToolOut(
            qualified_name=row.qualified_name,
            tool_name=row.tool_name,
            server=code,
            description=row.description,
            input_schema=row.input_schema,
            annotations=row.annotations,
            discovered_at=row.discovered_at,
        )
        for row in rows
    ]
