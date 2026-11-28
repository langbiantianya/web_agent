"""管理面 JSON API —— OAuth Provider（V4.10 阶段 E-1）。

锁住三件事：

1. **密钥只写不回显**：读取只有 ``hasClientSecret``，任何响应里都不该
   出现明文密钥；
2. **局部更新**：不传 ``clientSecret`` 表示保持原值，传空串表示清除；
3. **错误形状**：重复 code 409、不存在 404、缺 CSRF 403，且都带字段级文案。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.repo.oauth_provider_repo import oauth_provider_repo
from services.admin_web.accounts import create_super_admin_session
from services.admin_web.db import AdminSessionLocal
from services.admin_web.notifications import DeliveryResult
from services.admin_web.registry import seed_registry
from services.admin_web.security import super_csrf_cookie_name, super_session_cookie_name


def _password_sender():
    """构造总是成功的测试通知器。"""

    async def send(*, username, email, phone, password):
        """报告通知已送达。"""
        return DeliveryResult(email_status="sent", sms_status="skipped")

    return send


async def _client() -> httpx.AsyncClient:
    """建立带平台超级管理员会话的 ASGI 客户端。"""
    from services.admin_web.accounts import create_super_admin_account
    from services.admin_web.app import app as admin_asgi_app

    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        account, _ = await create_super_admin_account(
            db,
            username=f"api_oauth_{tag}",
            email=f"api-oauth-{tag}@example.com",
            phone=None,
            owner=False,
            password_sender=_password_sender(),
        )
        token, csrf = await create_super_admin_session(db, account)
        await db.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    return client


def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    """取 CSRF 头。"""
    return {"x-csrf-token": client.cookies.get(super_csrf_cookie_name()) or ""}


def _payload(code: str) -> dict:
    """构造一份合法的供应商配置。"""
    return {
        "code": code,
        "displayName": "演示供应商",
        "issuer": "https://id.example.com",
        "clientId": "client-1",
        "clientSecret": "s3cret-value",
        "scopes": ["openid", "profile"],
    }


async def test_oauth_provider_crud_round_trip() -> None:
    """创建 → 读取 → 更新 → 删除。"""
    client = await _client()
    code = f"op-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/api/oauth-providers", headers=_headers(client), json=_payload(code)
        )
        assert created.status_code == 201, created.text
        item = created.json()["item"]
        assert item["code"] == code
        assert item["hasClientSecret"] is True
        # **密钥不能回显**
        assert "s3cret-value" not in created.text
        assert "clientSecret" not in item

        listed = await client.get("/admin/api/oauth-providers")
        assert listed.status_code == 200
        assert any(
            row["code"] == code for row in listed.json()["items"]
        )

        updated = await client.put(
            f"/admin/api/oauth-providers/{code}",
            headers=_headers(client),
            json={"displayName": "改过的名字", "adminLoginEnabled": False},
        )
        assert updated.status_code == 200, updated.text
        updated_item = updated.json()["item"]
        assert updated_item["displayName"] == "改过的名字"
        assert updated_item["adminLoginEnabled"] is False
        # 没传密钥 → 保持原值
        assert updated_item["hasClientSecret"] is True

        toggled = await client.post(
            f"/admin/api/oauth-providers/{code}/toggle",
            headers=_headers(client),
            json={"enabled": False},
        )
        assert toggled.status_code == 200, toggled.text
        assert toggled.json()["item"]["enabled"] is False

        removed = await client.delete(
            f"/admin/api/oauth-providers/{code}", headers=_headers(client)
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["ok"] is True
    finally:
        await client.aclose()


async def test_oauth_provider_secret_can_be_cleared() -> None:
    """传空串表示清除密钥。"""
    client = await _client()
    code = f"op-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/api/oauth-providers", headers=_headers(client), json=_payload(code)
        )
        assert created.status_code == 201, created.text
        cleared = await client.put(
            f"/admin/api/oauth-providers/{code}",
            headers=_headers(client),
            json={"clientSecret": ""},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["item"]["hasClientSecret"] is False
    finally:
        async with AdminSessionLocal() as db:
            row = await oauth_provider_repo.get(db, code, "default")
            if row is not None:
                await oauth_provider_repo.delete(db, row)
                await db.commit()
        await client.aclose()


async def test_oauth_provider_rejects_duplicate_and_missing() -> None:
    """重复 code 409，不存在 404。"""
    client = await _client()
    code = f"op-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/api/oauth-providers", headers=_headers(client), json=_payload(code)
        )
        assert created.status_code == 201, created.text

        duplicated = await client.post(
            "/admin/api/oauth-providers", headers=_headers(client), json=_payload(code)
        )
        assert duplicated.status_code == 409, duplicated.text
        assert duplicated.json()["code"] == "duplicate"

        missing = await client.put(
            "/admin/api/oauth-providers/nope-not-here",
            headers=_headers(client),
            json={"displayName": "x"},
        )
        assert missing.status_code == 404, missing.text
    finally:
        async with AdminSessionLocal() as db:
            row = await oauth_provider_repo.get(db, code, "default")
            if row is not None:
                await oauth_provider_repo.delete(db, row)
                await db.commit()
        await client.aclose()


async def test_oauth_provider_reports_field_errors() -> None:
    """校验失败要指出**具体字段**，而不是丢一句通用提示。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/oauth-providers",
            headers=_headers(client),
            json={"code": "x", "displayName": "", "issuer": "short"},
        )
        assert response.status_code == 400, response.text
        payload = response.json()
        assert payload["code"] == "invalid_input"
        assert payload["fieldErrors"], payload
        # 字段名用 camelCase，与前端表单对齐
        assert "displayName" in payload["fieldErrors"]
        assert "issuer" in payload["fieldErrors"]
    finally:
        await client.aclose()


async def test_oauth_provider_write_requires_csrf() -> None:
    """写操作缺 CSRF 一律 403。"""
    client = await _client()
    code = f"op-{uuid.uuid4().hex[:8]}"
    try:
        response = await client.post(
            "/admin/api/oauth-providers", json=_payload(code)
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "invalid_csrf"
    finally:
        await client.aclose()
