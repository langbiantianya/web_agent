"""MCP OAuth machine identity, encrypted token cache and tenant isolation."""

from __future__ import annotations

import httpx
import pytest
from uuid import uuid4

from app.config.db import SessionLocal
from app.core.crypto import decrypt, encrypt
from app.core.mcp import oauth
from app.core.mcp.transport import http_headers
from app.main import create_app
from app.repo.mcp_repo import mcp_server_repo
from tests.conftest import authenticate_admin

ADMIN_HEADERS: dict[str, str] = {}


@pytest.fixture
async def client():
    """Real FastAPI app over ASGI transport."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as value:
            await authenticate_admin(value)
            yield value


async def _create_oauth_server(client, code: str) -> None:
    """Register a valid OAuth machine-identity server."""
    response = await client.post(
        "/api/mcp/servers",
        headers=ADMIN_HEADERS,
        json={
            "code": code,
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "auth_type": "oauth_client_credentials",
            "oauth_token_url": "https://auth.example.com/oauth/token",
            "oauth_revocation_url": "https://auth.example.com/oauth/revoke",
            "oauth_client_id": "machine-client",
            "oauth_client_secret": "super-secret-value",
            "oauth_scopes": ["mcp.read", "mcp.write"],
        },
    )
    assert response.status_code == 201, response.text
    assert "super-secret-value" not in response.text
    body = response.json()
    assert body["auth_type"] == "oauth_client_credentials"
    assert body["oauth_configured"] is True


async def test_oauth_secret_is_encrypted_and_only_reported_as_configured(client):
    """OAuth secret must never be stored or returned as plaintext."""
    code = "oauthapi"
    await _create_oauth_server(client, code)
    async with SessionLocal() as db:
        server = await mcp_server_repo.get_by_code(db, code)
        assert server is not None
        assert server.oauth_client_secret_cipher
        assert "super-secret-value" not in server.oauth_client_secret_cipher
        assert decrypt(server.oauth_client_secret_cipher) == "super-secret-value"
        assert server.tenant_id == "default"


async def test_oauth_token_is_cached_encrypted_and_refreshable(client, monkeypatch):
    """Access token is encrypted at rest and reused until expiry."""
    code = "oauthcache"
    await _create_oauth_server(client, code)
    calls = 0

    async def fake_token_request(server):
        """Return a deterministic token and count refresh requests."""
        nonlocal calls
        calls += 1
        return {"access_token": f"token-{calls}", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_request_token", fake_token_request)
    async with SessionLocal() as db:
        server = await mcp_server_repo.get_by_code(db, code)
        assert server is not None
        assert await oauth.access_token(server) == "token-1"
        assert await oauth.access_token(server) == "token-1"
        assert calls == 1
        stored = await mcp_server_repo.get_by_code(db, code)
        assert stored is not None
        assert stored.oauth_access_token_cipher
        assert "token-1" not in stored.oauth_access_token_cipher
        assert decrypt(stored.oauth_access_token_cipher) == "token-1"


async def test_http_headers_inject_bearer_without_leaking_static_auth(client, monkeypatch):
    """OAuth token replaces static Authorization while other headers remain."""
    code = "oauthheaders"
    await _create_oauth_server(client, code)
    async with SessionLocal() as db:
        server = await mcp_server_repo.get_by_code(db, code)
        assert server is not None
        server.headers = {"X-Tenant": encrypt("blue")}

    async def fake_access(_server):
        """Return a deterministic bearer token."""
        return "access-xyz"

    monkeypatch.setattr(oauth, "access_token", fake_access)
    headers = await http_headers(server)
    assert headers["Authorization"] == "Bearer access-xyz"
    assert headers["X-Tenant"] == "blue"


async def test_revoke_clears_local_tokens_without_remote_endpoint(client):
    """Local revocation must still work when provider has no revocation endpoint."""
    code = "oauthrevoke"
    await _create_oauth_server(client, code)
    async with SessionLocal() as db:
        server = await mcp_server_repo.get_by_code(db, code)
        assert server is not None
        server.oauth_access_token_cipher = encrypt("temporary")
        server.oauth_refresh_token_cipher = encrypt("refresh")
        server.oauth_revocation_url = None
        await db.commit()
        await oauth.revoke(server)
        refreshed = await mcp_server_repo.get_by_code(db, code)
        assert refreshed is not None
        assert refreshed.oauth_access_token_cipher is None
        assert refreshed.oauth_refresh_token_cipher is None


async def test_oauth_requires_token_url_client_and_secret(client):
    """OAuth without all machine-identity fields is rejected before storage."""
    response = await client.post(
        "/api/mcp/servers",
        headers=ADMIN_HEADERS,
        json={
            "code": "oauthmissing",
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "auth_type": "oauth_client_credentials",
        },
    )
    assert response.status_code == 400
    assert "oauth_token_url" in response.text


async def test_mcp_servers_are_isolated_by_admin_tenant(client):
    """A MCP server created in one tenant is invisible to another tenant."""
    suffix = uuid4().hex[:8]
    tenant = f"tenant-{suffix}"
    tenant_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        timeout=30,
    )
    await authenticate_admin(
        tenant_client,
        tenant_id=tenant,
        platform_super=False,
        roles=("tenant_admin",),
    )
    code = f"tenantmcp{suffix}"
    try:
        created = await tenant_client.post(
            "/api/mcp/servers",
            json={
                "code": code,
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == tenant
        assert (
            await client.get(f"/api/mcp/servers/{code}")
        ).status_code == 404
        listing = await client.get("/api/mcp/servers")
        assert code not in {item["code"] for item in listing.json()}
    finally:
        await tenant_client.aclose()
