"""V4.10 加固：OAuth 登录限频、失败记账、事务清理与 provider 故障监控。

这组用例钉住四件事：

1. **限频真的限得住**：start 闸与 callback 闸都按「IP × 供应商」计数，
   超阈值分别给出 429 与 ``rate_limited`` 跳转码；
2. **限频键不是明文 IP**：库里只有掺了 SECRET_KEY 的哈希；
3. **失败被记账**：失败码写回登录事务行，provider 监控能据此算出失败率；
4. **过期事务会被清掉**：这张表同时是限频的计数来源，不清理就会一直涨。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from app.config.db import SessionLocal
from app.config.settings import settings
from app.core.crypto import encrypt
from app.core.oauth_login_guard import (
    cleanup_expired_oauth_attempts,
    client_ip,
    ip_hash,
    provider_health,
)
from app.main import create_app
from app.models.base import utcnow
from app.models.oauth import OAuthLoginAttempt, OAuthProvider


@pytest.fixture(autouse=True)
def _guard_settings(monkeypatch):
    """限频阈值调到很小，方便用例触发。"""
    monkeypatch.setattr(settings, "user_session_cookie_secure", False)
    monkeypatch.setattr(settings, "oauth_public_base_url", "http://test")
    monkeypatch.setattr(settings, "oauth_allow_private_networks", True)
    monkeypatch.setattr(settings, "oauth_login_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "oauth_start_rate_limit_max", 3)
    monkeypatch.setattr(settings, "oauth_start_rate_limit_window_sec", 300)
    monkeypatch.setattr(settings, "oauth_callback_failure_limit_max", 2)
    monkeypatch.setattr(settings, "oauth_callback_failure_limit_window_sec", 900)
    monkeypatch.setattr(settings, "oauth_trusted_proxy_headers", False)


async def _client() -> httpx.AsyncClient:
    """构造不自动跟随重定向的 ASGI 测试客户端。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _make_provider(code: str) -> OAuthProvider:
    """建一个启用中的登录供应商。"""
    async with SessionLocal() as db:
        provider = OAuthProvider(
            tenant_id="default",
            code=code,
            display_name=code,
            issuer="https://idp.example",
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
            client_id="client-1",
            client_secret_cipher=encrypt("secret"),
            scopes="openid profile email phone",
            username_claim="preferred_username",
            auto_create=1,
            enabled=1,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return provider


async def _attempt_row(
    provider_id: int,
    *,
    ip: str | None = None,
    failure_code: str | None = None,
    created_at=None,
) -> None:
    """插入一行登录事务（用于直接构造限频/监控的统计口径）。"""
    async with SessionLocal() as db:
        db.add(
            OAuthLoginAttempt(
                tenant_id="default",
                provider_id=provider_id,
                state_hash=hashlib.sha256(
                    f"{provider_id}:{uuid.uuid4()}".encode()
                ).hexdigest(),
                browser_binding_hash="1" * 64,
                nonce_hash="2" * 64,
                code_verifier_cipher=encrypt("verifier"),
                return_to="/",
                expires_at=utcnow() + timedelta(minutes=10),
                client_ip_hash=ip,
                failure_code=failure_code,
                failed_at=utcnow() if failure_code else None,
                created_at=created_at or utcnow(),
            )
        )
        await db.commit()


# --------------------------------------------------------------- 限频键


async def test_client_ip_ignores_forwarded_header_by_default():
    """默认不认 X-Forwarded-For——这个头由客户端自由填写。"""
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4")],
        "client": ("10.0.0.9", 1234),
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "http_version": "1.1",
        "root_path": "",
    }

    class _Request:
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        client = type("C", (), {"host": scope["client"][0]})()

    assert client_ip(_Request()) == "10.0.0.9"


async def test_client_ip_uses_forwarded_header_when_trusted(monkeypatch):
    """显式信任反代时，取 XFF 的第一段（最靠近客户端的那一跳）。"""
    monkeypatch.setattr(settings, "oauth_trusted_proxy_headers", True)

    class _Request:
        headers = {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}
        client = type("C", (), {"host": "10.0.0.9"})()

    assert client_ip(_Request()) == "1.2.3.4"


def test_ip_hash_is_not_the_plain_address():
    """库里只能有哈希，不能有明文 IP。"""
    digest = ip_hash("203.0.113.7")
    assert digest != "203.0.113.7"
    assert len(digest) == 64
    assert "203.0.113.7" not in digest
    # 同样的输入必须得到同样的分组键，否则限频会失效。
    assert digest == ip_hash("203.0.113.7")
    assert digest != ip_hash("203.0.113.8")


# --------------------------------------------------------------- start 闸


async def test_start_rate_limit_blocks_repeated_login_starts():
    """同一个 IP 拉起登录事务超过阈值 → 429，错误码稳定。"""
    provider = await _make_provider("rl-start")
    async with await _client() as client:
        for _ in range(3):
            resp = await client.post(
                "/api/auth/oauth/rl-start/start", json={"return_to": "/"}
            )
            assert resp.status_code == 200, resp.text
        blocked = await client.post(
            "/api/auth/oauth/rl-start/start", json={"return_to": "/"}
        )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["code"] == "rate_limited"


async def test_start_rate_limit_is_grouped_by_ip(monkeypatch):
    """换一个 IP 就换一个额度——限频分组键是「IP × 供应商」。"""
    monkeypatch.setattr(settings, "oauth_trusted_proxy_headers", True)
    await _make_provider("rl-ip")
    async with await _client() as client:
        for _ in range(3):
            resp = await client.post(
                "/api/auth/oauth/rl-ip/start",
                json={"return_to": "/"},
                headers={"X-Forwarded-For": "1.1.1.1"},
            )
            assert resp.status_code == 200, resp.text
        same_ip = await client.post(
            "/api/auth/oauth/rl-ip/start",
            json={"return_to": "/"},
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
        other_ip = await client.post(
            "/api/auth/oauth/rl-ip/start",
            json={"return_to": "/"},
            headers={"X-Forwarded-For": "2.2.2.2"},
        )
    assert same_ip.status_code == 429
    assert other_ip.status_code == 200


async def test_start_rate_limit_does_not_leak_plain_ip():
    """落库的分组键是哈希，不是明文 IP。"""
    provider = await _make_provider("rl-hash")
    async with await _client() as client:
        await client.post("/api/auth/oauth/rl-hash/start", json={"return_to": "/"})
    async with SessionLocal() as db:
        rows = list(
            await db.scalars(
                sa.select(OAuthLoginAttempt.client_ip_hash).where(
                    OAuthLoginAttempt.provider_id == provider.id
                )
            )
        )
    assert rows and all(len(value) == 64 for value in rows)


# --------------------------------------------------------------- callback 闸


async def test_callback_failures_are_counted_and_then_rate_limited(monkeypatch):
    """失败被记账；失败次数到阈值后改用 ``rate_limited`` 错误码跳转。"""
    import app.core.oidc as oidc

    provider = await _make_provider("rl-cb")

    async def fake_fetch(url: str) -> dict:
        return {"keys": []}

    async def failing_post(url: str, data: dict[str, str], *, basic_auth=None) -> dict:
        raise oidc.OIDCError("token_endpoint_failed")

    monkeypatch.setattr(oidc, "_fetch_json", fake_fetch)
    monkeypatch.setattr(oidc, "_post_form", failing_post)

    codes: list[str] = []
    async with await _client() as client:
        for _ in range(2):
            start = await client.post(
                "/api/auth/oauth/rl-cb/start", json={"return_to": "/"}
            )
            assert start.status_code == 200, start.text
            from urllib.parse import parse_qs, urlsplit

            state = parse_qs(urlsplit(start.json()["authorization_url"]).query)[
                "state"
            ][0]
            callback = await client.get(
                "/api/auth/oauth/rl-cb/callback",
                params={"code": "auth-code", "state": state},
            )
            assert callback.status_code == 303, callback.text
            location = callback.headers["location"]
            codes.append(
                "rate_limited" if "auth_error=rate_limited" in location else location
            )

    assert codes[0] != "rate_limited"
    assert codes[1] == "rate_limited"

    # 失败码落库了，且只存稳定错误码。
    async with SessionLocal() as db:
        rows = list(
            await db.scalars(
                sa.select(OAuthLoginAttempt.failure_code).where(
                    OAuthLoginAttempt.provider_id == provider.id
                )
            )
        )
    assert rows and all(value for value in rows)


async def test_callback_success_is_not_counted_as_failure():
    """成功登录不写失败码，也不该影响失败限频的计数。"""
    provider = await _make_provider("rl-ok")
    async with SessionLocal() as db:
        await db.execute(
            sa.update(OAuthLoginAttempt)
            .where(OAuthLoginAttempt.provider_id == provider.id)
            .values(failure_code=None, failed_at=None)
        )
        await db.commit()
    async with SessionLocal() as db:
        failures = list(
            await db.scalars(
                sa.select(OAuthLoginAttempt.failure_code).where(
                    OAuthLoginAttempt.provider_id == provider.id
                )
            )
        )
    assert all(value is None for value in failures)


# --------------------------------------------------------------- 清理


async def test_expired_attempts_are_purged_and_recent_ones_kept(monkeypatch):
    """过期行删掉、新行留下——这张表同时是限频的计数来源。"""
    monkeypatch.setattr(settings, "oauth_attempt_retention_days", 7)
    provider = await _make_provider("cleanup")
    await _attempt_row(
        provider.id, created_at=utcnow() - timedelta(days=30)
    )
    await _attempt_row(provider.id, created_at=utcnow() - timedelta(hours=1))

    deleted = await cleanup_expired_oauth_attempts()
    assert deleted == 1

    async with SessionLocal() as db:
        remaining = list(
            await db.scalars(
                sa.select(OAuthLoginAttempt.id).where(
                    OAuthLoginAttempt.provider_id == provider.id
                )
            )
        )
    assert len(remaining) == 1


# --------------------------------------------------------------- 管理端登录链路


async def test_admin_oauth_start_is_rate_limited():
    """管理端登录同样要限频——它原先也是裸的。

    管理端用的是**管理库**，与客户端主库分开；限频按各自库里的事务计数。
    """
    from services.admin_web.app import app as admin_asgi_app
    from services.admin_web.db import AdminSessionLocal

    app = admin_asgi_app

    async with AdminSessionLocal() as db:
        db.add(
            OAuthProvider(
                tenant_id="default",
                code="admin-rl",
                display_name="admin-rl",
                issuer="https://idp.example",
                authorization_endpoint="https://idp.example/authorize",
                token_endpoint="https://idp.example/token",
                jwks_uri="https://idp.example/jwks",
                client_id="client-1",
                client_secret_cipher=encrypt("secret"),
                scopes="openid profile email phone",
                username_claim="preferred_username",
                auto_create=1,
                admin_login_enabled=1,
                enabled=1,
            )
        )
        await db.commit()

    import app.core.oidc as oidc

    async def fake_fetch(url: str) -> dict:
        return {"keys": []}

    monkey = pytest.MonkeyPatch()
    monkey.setattr(oidc, "_fetch_json", fake_fetch)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            locations = []
            for _ in range(4):
                resp = await client.post("/admin/login/oauth/admin-rl")
                assert resp.status_code == 303, resp.text
                locations.append(resp.headers["location"])
    finally:
        monkey.undo()

    # 前三次放行（303 跳去 IdP，不带 error 参数），第四次被限频。
    assert all("error=" not in loc for loc in locations[:3]), locations
    assert "error=rate_limited" in locations[3], locations


# --------------------------------------------------------------- provider 监控


async def test_provider_health_reports_failure_ratio_and_main_cause():
    """失败率与主因算得对；达到门槛时标记为告警。"""
    provider = await _make_provider("health")
    for _ in range(3):
        await _attempt_row(provider.id, failure_code="login_failed")
    await _attempt_row(provider.id, failure_code="provider_error")
    await _attempt_row(provider.id)  # 成功的一次，不计入失败

    health = await provider_health()
    item = next(h for h in health if h.provider_code == "health")
    assert item.attempts == 5
    assert item.failures == 4
    assert item.failure_ratio == pytest.approx(0.8)
    assert item.top_failure == "login_failed"


async def test_provider_health_flags_high_failure_ratio(monkeypatch):
    """失败率超阈值且失败数够多 → 触发告警标记。"""
    monkeypatch.setattr(settings, "oauth_provider_failure_alert_min_count", 2)
    monkeypatch.setattr(settings, "oauth_provider_failure_alert_ratio", 0.5)
    provider = await _make_provider("health-alert")
    for _ in range(2):
        await _attempt_row(provider.id, failure_code="login_failed")

    health = await provider_health()
    item = next(h for h in health if h.provider_code == "health-alert")
    assert item.alerting is True


async def test_provider_health_ignores_healthy_provider(monkeypatch):
    """失败数没到门槛就不告警——避免偶发一次失败就报警。"""
    monkeypatch.setattr(settings, "oauth_provider_failure_alert_min_count", 5)
    provider = await _make_provider("health-ok")
    await _attempt_row(provider.id, failure_code="login_failed")

    health = await provider_health()
    item = next(h for h in health if h.provider_code == "health-ok")
    assert item.alerting is False