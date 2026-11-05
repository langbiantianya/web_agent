"""MCP OAuth 2.0 机器身份、令牌缓存与撤销。

当前实现支持标准的 ``client_credentials`` 与 ``refresh_token`` grant。令牌和
client secret 都使用应用 ``SECRET_KEY`` 派生的 Fernet 密钥加密；调用日志、
API 响应和工具结果都不会出现明文。

交互式 authorization-code 流程需要浏览器回调和用户同意页面，本实现不在
后端 API 中伪造这条流程。需要用户委托的 MCP 服务应使用支持机器身份的令牌端点，
或在后续版本接入独立的 OAuth 授权前端。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, timedelta
from typing import Any

from app.core.crypto import decrypt, encrypt
from app.models.base import utcnow

logger = logging.getLogger(__name__)

_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


class McpOAuthError(Exception):
    """OAuth 配置、令牌请求或响应处理失败。"""


def _lock_for(tenant_id: str, code: str) -> asyncio.Lock:
    """取同一 MCP 服务器的令牌刷新锁，避免并发请求打出多份 token。"""
    key = (tenant_id or "default", code)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _token_is_fresh(server) -> bool:
    """缓存 token 是否仍有效（预留 30 秒时钟偏差）。"""
    if not server.oauth_access_token_cipher or server.oauth_expires_at is None:
        return False
    expires = server.oauth_expires_at
    if getattr(expires, "tzinfo", None) is not None:
        expires = expires.astimezone(UTC).replace(tzinfo=None)
    return expires > utcnow() + timedelta(seconds=30)


async def _check_token_url(url: str) -> None:
    """令牌端点与 MCP 端点使用同一套 SSRF 策略。"""
    from app.core.mcp.transport import McpTransportError, check_url_allowed

    if not url:
        raise McpOAuthError("缺少 OAuth token endpoint")
    try:
        await check_url_allowed(url)
    except McpTransportError as exc:
        raise McpOAuthError(str(exc)) from exc


async def _request_token(
    server,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """请求 access token。"""
    import httpx2

    await _check_token_url(str(server.oauth_token_url or ""))
    client_secret = decrypt(server.oauth_client_secret_cipher)
    if not client_secret or not server.oauth_client_id:
        raise McpOAuthError("OAuth client_id/client_secret 未配置")
    refresh_token = decrypt(server.oauth_refresh_token_cipher)
    form: dict[str, str] = {}
    if refresh_token:
        form["grant_type"] = "refresh_token"
        form["refresh_token"] = refresh_token
    else:
        form["grant_type"] = "client_credentials"
    scopes = [str(item).strip() for item in (server.oauth_scopes or []) if str(item).strip()]
    if scopes:
        form["scope"] = " ".join(scopes)
    if server.oauth_resource:
        form["resource"] = str(server.oauth_resource)

    owns_client = client is None
    if client is None:
        client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(float(server.timeout_sec or 30)),
            follow_redirects=False,
        )
    try:
        response = await client.post(
            server.oauth_token_url,
            data=form,
            auth=httpx2.BasicAuth(
                str(server.oauth_client_id), client_secret
            ),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
    except Exception as exc:
        raise McpOAuthError(f"请求 OAuth token 失败：{exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code >= 400:
        raise McpOAuthError(
            f"OAuth token endpoint 返回 {response.status_code}："
            f"{response.text[:300]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise McpOAuthError("OAuth token endpoint 返回的不是 JSON") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise McpOAuthError("OAuth token 响应缺少 access_token")
    return data


async def _persist_token(server, data: dict[str, Any]) -> str:
    """加密保存 token，并返回明文 access token 给调用方。"""
    import sqlalchemy as sa

    from app.config.db import SessionLocal
    from app.models.mcp_server import McpServer

    access = str(data["access_token"])
    refresh_value = data.get("refresh_token")
    refresh = (
        str(refresh_value)
        if refresh_value
        else decrypt(server.oauth_refresh_token_cipher)
    )
    try:
        expires_in = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    expires_at = utcnow() + timedelta(seconds=max(60, expires_in))
    async with SessionLocal() as db:
        await db.execute(
            sa.update(McpServer)
            .where(
                McpServer.id == server.id,
                McpServer.tenant_id == (server.tenant_id or "default"),
            )
            .values(
                oauth_access_token_cipher=encrypt(access),
                oauth_refresh_token_cipher=encrypt(refresh) if refresh else None,
                oauth_expires_at=expires_at,
            )
        )
        await db.commit()
    server.oauth_access_token_cipher = encrypt(access)
    server.oauth_refresh_token_cipher = encrypt(refresh) if refresh else None
    server.oauth_expires_at = expires_at
    return access


async def access_token(server) -> str:
    """取一个可用 access token；过期时在同一服务器的单飞锁内刷新。"""
    async with _lock_for(server.tenant_id or "default", server.code):
        if _token_is_fresh(server):
            token = decrypt(server.oauth_access_token_cipher)
            if token:
                return token
        data = await _request_token(server)
        return await _persist_token(server, data)


async def revoke(server) -> bool:
    """尽力调用 RFC 7009 撤销端点，并无论远端结果如何清掉本地令牌。"""
    import sqlalchemy as sa

    from app.config.db import SessionLocal
    from app.models.mcp_server import McpServer

    token = decrypt(server.oauth_access_token_cipher)
    remote_ok = True
    if token and server.oauth_revocation_url:
        import httpx2

        try:
            await _check_token_url(str(server.oauth_revocation_url))
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(float(server.timeout_sec or 30)),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    server.oauth_revocation_url,
                    data={"token": token, "token_type_hint": "access_token"},
                    auth=httpx2.BasicAuth(
                        str(server.oauth_client_id or ""),
                        decrypt(server.oauth_client_secret_cipher),
                    ),
                )
                remote_ok = response.status_code < 400
        except Exception:
            logger.warning(
                "MCP OAuth 远端撤销失败：%s", server.code, exc_info=True
            )
            remote_ok = False

    async with SessionLocal() as db:
        await db.execute(
            sa.update(McpServer)
            .where(
                McpServer.id == server.id,
                McpServer.tenant_id == (server.tenant_id or "default"),
            )
            .values(
                oauth_access_token_cipher=None,
                oauth_refresh_token_cipher=None,
                oauth_expires_at=None,
            )
        )
        await db.commit()
    server.oauth_access_token_cipher = None
    server.oauth_refresh_token_cipher = None
    server.oauth_expires_at = None
    return remote_ok
