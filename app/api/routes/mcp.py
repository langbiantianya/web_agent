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

from app.api.deps import (
    DbSession,
    McpReadDep,
    McpRefreshDep,
    McpWriteDep,
)
from app.core.crypto import encrypt
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.manager import mcp_manager
from app.core.mcp.oauth import McpOAuthError, revoke as revoke_oauth
from app.core.mcp.transport import McpTransportError, check_command_allowed, check_url_allowed
from app.models.mcp_server import TRANSPORT_HTTP, TRANSPORT_STDIO
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.schemas.mcp import (
    McpPromptGetOut,
    McpPromptGetIn,
    McpPromptOut,
    McpOAuthRevokeOut,
    McpRefreshOut,
    McpResourceContentOut,
    McpResourceOut,
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
        tenant_id=server.tenant_id or "default",
        name=server.name or server.code,
        transport=server.transport,
        command=server.command,
        args=list(server.args or []) or None,
        env_keys=sorted((server.env or {}).keys()),
        url=server.url,
        header_keys=sorted((server.headers or {}).keys()),
        auth_type=server.auth_type or "static",
        oauth_token_url=server.oauth_token_url,
        oauth_revocation_url=server.oauth_revocation_url,
        oauth_client_id=server.oauth_client_id,
        oauth_scopes=list(server.oauth_scopes or []) or None,
        oauth_resource=server.oauth_resource,
        oauth_configured=(
            server.auth_type == "oauth_client_credentials"
            and bool(
                server.oauth_client_secret_cipher
                or server.oauth_access_token_cipher
            )
        ),
        oauth_token_expires_at=server.oauth_expires_at,
        allow_tools=list(server.allow_tools or []) or None,
        timeout_sec=server.timeout_sec or 30,
        enabled=bool(server.enabled),
        version=server.version or 1,
        # “没有失败标记”不等于“当前有连接”：空闲巡检会主动关闭 stdio/远端连接，
        # 下一次调用再惰性重连。这里展示真实的进程内连接状态。
        online=mcp_manager.is_connected(server.code),
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
        if fields.get("auth_type") not in (None, "none", "static"):
            raise BadRequestError("stdio 传输不支持 OAuth 认证字段")
    elif transport == TRANSPORT_HTTP:
        if not fields.get("url"):
            raise BadRequestError("http 传输必须提供 url")
        if fields.get("command"):
            raise BadRequestError("http 传输不接受 command")
        auth_type = fields.get("auth_type") or "static"
        if auth_type == "oauth_client_credentials":
            if not fields.get("oauth_token_url"):
                raise BadRequestError("OAuth 机器身份必须提供 oauth_token_url")
            if not fields.get("oauth_client_id"):
                raise BadRequestError("OAuth 机器身份必须提供 oauth_client_id")
            if not (
                fields.get("oauth_client_secret")
                or fields.get("oauth_client_secret_cipher")
            ):
                raise BadRequestError("OAuth 机器身份必须提供 oauth_client_secret")
            if any(
                str(key).lower() == "authorization"
                for key in (fields.get("headers") or {})
            ):
                raise BadRequestError(
                    "OAuth 认证由服务端注入 Authorization，headers 不能重复配置"
                )


@router.get("", response_model=list[McpServerOut])
async def list_servers(db: DbSession, admin: McpReadDep) -> list[McpServerOut]:
    """列出全部 MCP 服务器 + 在线状态 + 工具数。"""
    servers = await mcp_server_repo.list(
        db, tenant_id=admin.tenant_id, limit=500
    )
    out: list[McpServerOut] = []
    for server in servers:
        tools = await mcp_tool_repo.list_by_server(db, server.id)
        out.append(_to_out(server, len(tools)))
    return out


@router.post("", response_model=McpServerOut, status_code=201)
async def create_server(
    payload: McpServerCreate, db: DbSession, admin: McpWriteDep
) -> McpServerOut:
    """注册一台 MCP 服务器。

    **这是部署级操作**：stdio 的 ``command`` 决定执行什么程序，所以命令路径
    必须在 ``MCP_ALLOWED_COMMAND_PREFIXES`` 白名单内（默认空 = 禁止）。
    """
    if await mcp_server_repo.get_by_code(db, payload.code, admin.tenant_id):
        raise ConflictError(f"MCP 服务器 code 已存在：{payload.code}")
    fields = {
        "command": payload.command,
        "args": payload.args,
        "url": payload.url,
        "auth_type": payload.auth_type,
        "oauth_token_url": payload.oauth_token_url,
        "oauth_revocation_url": payload.oauth_revocation_url,
        "oauth_client_id": payload.oauth_client_id,
        "oauth_client_secret": payload.oauth_client_secret,
        "headers": payload.headers,
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
        if payload.auth_type == "oauth_client_credentials":
            for url in (
                payload.oauth_token_url,
                payload.oauth_revocation_url,
            ):
                if not url:
                    continue
                try:
                    await check_url_allowed(url, require_dns=False)
                except McpTransportError as exc:
                    raise BadRequestError(str(exc)) from exc

    server = await mcp_server_repo.create(
        db,
        code=payload.code,
        tenant_id=admin.tenant_id,
        name=payload.name or payload.code,
        transport=payload.transport,
        command=payload.command,
        args=payload.args,
        env=_encrypt_map(payload.env),
        url=payload.url,
        headers=_encrypt_map(payload.headers),
        auth_type=payload.auth_type,
        oauth_token_url=payload.oauth_token_url,
        oauth_revocation_url=payload.oauth_revocation_url,
        oauth_client_id=payload.oauth_client_id,
        oauth_client_secret_cipher=(
            encrypt(payload.oauth_client_secret)
            if payload.auth_type == "oauth_client_credentials"
            and payload.oauth_client_secret
            else None
        ),
        oauth_scopes=payload.oauth_scopes,
        oauth_resource=payload.oauth_resource,
        allow_tools=payload.allow_tools,
        timeout_sec=payload.timeout_sec,
        enabled=1 if payload.enabled else 0,
    )
    return _to_out(server, 0)


@router.get("/{code}", response_model=McpServerOut)
async def get_server(
    code: str, db: DbSession, admin: McpReadDep
) -> McpServerOut:
    """查一台服务器。"""
    server = await mcp_server_repo.get_by_code(db, code, admin.tenant_id)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    tools = await mcp_tool_repo.list_by_server(db, server.id)
    return _to_out(server, len(tools))


@router.put("/{code}", response_model=McpServerOut)
async def update_server(
    code: str, payload: McpServerUpdate, db: DbSession, admin: McpWriteDep
) -> McpServerOut:
    """更新服务器：``version+1``，并**失效连接与快照**。

    连接与快照都要失效：改了命令或端点之后，旧连接连的是旧程序，旧快照描述
    的是旧工具——留着它们就是「配置说 A、实际跑 B」。
    """
    server = await mcp_server_repo.get_by_code(db, code, admin.tenant_id)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    fields = payload.model_dump(exclude_unset=True)
    if "env" in fields:
        fields["env"] = _encrypt_map(fields["env"])
    if "headers" in fields:
        fields["headers"] = _encrypt_map(fields["headers"])
    if "oauth_client_secret" in fields:
        changed_secret = fields.pop("oauth_client_secret")
        if changed_secret:
            fields["oauth_client_secret_cipher"] = encrypt(changed_secret)
    for oauth_url_key in ("oauth_token_url", "oauth_revocation_url"):
        if fields.get(oauth_url_key):
            try:
                await check_url_allowed(
                    fields[oauth_url_key], require_dns=False
                )
            except McpTransportError as exc:
                raise BadRequestError(str(exc)) from exc
    effective_auth = fields.get("auth_type", server.auth_type or "static")
    if effective_auth == "oauth_client_credentials":
        merged = {
            "auth_type": effective_auth,
            "oauth_token_url": fields.get(
                "oauth_token_url", server.oauth_token_url
            ),
            "oauth_client_id": fields.get(
                "oauth_client_id", server.oauth_client_id
            ),
            "oauth_client_secret": fields.get("oauth_client_secret"),
            "oauth_client_secret_cipher": fields.get(
                "oauth_client_secret_cipher", server.oauth_client_secret_cipher
            ),
            "headers": fields.get("headers", server.headers),
        }
        _check_transport_fields(TRANSPORT_HTTP, merged)
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
    invalidating_keys = {
        "command", "args", "env", "url", "headers", "auth_type",
        "oauth_token_url", "oauth_revocation_url", "oauth_client_id",
        "oauth_client_secret_cipher", "oauth_scopes", "oauth_resource",
        "allow_tools",
    }
    changes_connection = bool(invalidating_keys.intersection(fields))
    disabling = fields.get("enabled") == 0
    if (
        changes_connection or disabling
    ) and await mcp_server_repo.agents_using(db, server, admin.tenant_id):
        raise ConflictError(
            "该 MCP 连接仍被 Agent 使用，请先解绑相关工具再修改连接或停用"
        )
    updated = await mcp_server_repo.update(
        db, code, fields, bump_version=True, tenant_id=admin.tenant_id
    )
    if updated is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    # 连接配置变了才清空快照；仅改名或调整超时不改变工具目录。
    if changes_connection:
        await mcp_manager.close_all()
        await mcp_tool_repo.replace_for_server(db, updated.id, [])
    tools = await mcp_tool_repo.list_by_server(db, updated.id)
    return _to_out(updated, len(tools))


@router.delete("/{code}", status_code=204)
async def delete_server(
    code: str,
    db: DbSession,
    admin: McpWriteDep,
) -> None:
    """删除服务器（连带删掉它的工具快照）。"""
    server = await mcp_server_repo.get_by_code(db, code, admin.tenant_id)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    if await mcp_server_repo.agents_using(db, server, admin.tenant_id):
        raise ConflictError("该 MCP 连接仍被 Agent 使用，不能删除")
    ok = await mcp_server_repo.delete(db, code, admin.tenant_id)
    if not ok:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    await mcp_manager.close_all()


@router.post("/{code}/refresh", response_model=McpRefreshOut)
async def refresh_server(
    code: str,
    db: DbSession,
    admin: McpRefreshDep,
) -> McpRefreshOut:
    """重连并刷新工具快照。

    这是「MCP 服务器从来没连过 → 保存期校验直接 400」那条提示的解法：先刷新
    一次目录，工具进快照了，Agent 才能绑。
    """
    server = await mcp_server_repo.get_by_code(db, code, admin.tenant_id)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    try:
        result = await refresh_catalog(db, server)
    except McpCatalogError as exc:
        raise BadRequestError(str(exc)) from exc
    return McpRefreshOut(
        server=code, imported=result.imported, skipped=result.as_dict()["skipped"]
    )


@router.post("/{code}/oauth/revoke", response_model=McpOAuthRevokeOut)
async def revoke_server_oauth(
    code: str, db: DbSession, admin: McpWriteDep
) -> McpOAuthRevokeOut:
    """撤销 OAuth token：清本地密文，并尽力通知远端撤销端点。"""
    server = await mcp_server_repo.get_by_code(db, code, admin.tenant_id)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    if server.auth_type != "oauth_client_credentials":
        raise BadRequestError("该服务器没有使用 OAuth 机器身份")
    try:
        remote_revoked = await revoke_oauth(server)
    except McpOAuthError as exc:
        raise BadRequestError(str(exc)) from exc
    await mcp_manager.close_all()
    return McpOAuthRevokeOut(
        server=code,
        local_tokens_cleared=True,
        remote_revoked=remote_revoked,
    )


@router.get("/{code}/tools", response_model=list[McpToolOut])
async def list_server_tools(
    code: str, db: DbSession, admin: McpReadDep
) -> list[McpToolOut]:
    """该服务器**已发现**的工具（快照，不是实时）。"""
    server = await mcp_server_repo.get_by_code(db, code, admin.tenant_id)
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


def _model_json(value) -> dict:
    """把 MCP SDK 模型转成 JSON 字典，兼容字段别名。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return dict(value or {})


async def _server_or_404(db, code: str, admin: McpReadDep):
    """读取启用的 MCP 服务器配置。"""
    server = await mcp_server_repo.get_by_code(db, code, admin.tenant_id)
    if server is None:
        raise NotFoundError(f"MCP 服务器不存在：{code}")
    if not server.enabled:
        raise BadRequestError(f"MCP 服务器「{code}」已停用")
    return server


@router.get("/{code}/resources", response_model=list[McpResourceOut])
async def list_server_resources(
    code: str, db: DbSession, admin: McpReadDep
) -> list[McpResourceOut]:
    """实时列出 MCP 资源；该接口受管理员门禁保护。"""
    server = await _server_or_404(db, code, admin)
    try:
        rows = await mcp_manager.list_resources(server)
    except Exception as exc:
        raise BadRequestError(f"读取 MCP 资源列表失败：{exc}") from exc
    return [
        McpResourceOut(
            uri=str(getattr(row, "uri", "")),
            name=str(getattr(row, "name", "") or ""),
            description=getattr(row, "description", None),
            mime_type=getattr(row, "mime_type", None),
            size=getattr(row, "size", None),
        )
        for row in rows
    ]


@router.get(
    "/{code}/resource-templates", response_model=list[McpResourceOut]
)
async def list_server_resource_templates(
    code: str, db: DbSession, admin: McpReadDep
) -> list[McpResourceOut]:
    """实时列出 MCP 资源模板。"""
    server = await _server_or_404(db, code, admin)
    try:
        rows = await mcp_manager.list_resource_templates(server)
    except Exception as exc:
        raise BadRequestError(f"读取 MCP 资源模板失败：{exc}") from exc
    return [
        McpResourceOut(
            uri=str(getattr(row, "uri_template", "")),
            name=str(getattr(row, "name", "") or ""),
            description=getattr(row, "description", None),
            mime_type=getattr(row, "mime_type", None),
        )
        for row in rows
    ]


@router.get(
    "/{code}/resources/read", response_model=list[McpResourceContentOut]
)
async def read_server_resource(
    code: str,
    db: DbSession,
    admin: McpReadDep,
    uri: str,
) -> list[McpResourceContentOut]:
    """读取资源 URI；文本和 base64 二进制分别返回。"""
    server = await _server_or_404(db, code, admin)
    try:
        result = await mcp_manager.read_resource(server, uri)
    except Exception as exc:
        raise BadRequestError(f"读取 MCP 资源失败：{exc}") from exc
    out: list[McpResourceContentOut] = []
    for item in getattr(result, "contents", None) or []:
        out.append(
            McpResourceContentOut(
                uri=str(getattr(item, "uri", uri)),
                mime_type=getattr(item, "mime_type", None),
                text=getattr(item, "text", None),
                blob_base64=getattr(item, "blob", None),
            )
        )
    return out


@router.get("/{code}/prompts", response_model=list[McpPromptOut])
async def list_server_prompts(
    code: str, db: DbSession, admin: McpReadDep
) -> list[McpPromptOut]:
    """实时列出 MCP prompts。"""
    server = await _server_or_404(db, code, admin)
    try:
        rows = await mcp_manager.list_prompts(server)
    except Exception as exc:
        raise BadRequestError(f"读取 MCP prompt 列表失败：{exc}") from exc
    return [
        McpPromptOut(
            name=str(getattr(row, "name", "")),
            description=getattr(row, "description", None),
            arguments=[
                _model_json(item)
                for item in (getattr(row, "arguments", None) or [])
            ],
        )
        for row in rows
    ]


@router.post("/{code}/prompts/{prompt}/get", response_model=McpPromptGetOut)
async def get_server_prompt(
    code: str,
    prompt: str,
    body: McpPromptGetIn,
    db: DbSession,
    admin: McpReadDep,
) -> McpPromptGetOut:
    """获取 prompt；参数只传字符串，符合 MCP 规范。"""
    server = await _server_or_404(db, code, admin)
    try:
        result = await mcp_manager.get_prompt(server, prompt, body.arguments)
    except Exception as exc:
        raise BadRequestError(f"获取 MCP prompt 失败：{exc}") from exc
    return McpPromptGetOut(
        description=getattr(result, "description", None),
        messages=[
            _model_json(item)
            for item in (getattr(result, "messages", None) or [])
        ],
    )
