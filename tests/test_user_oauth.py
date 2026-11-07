"""V4.6：用户 OIDC 登录、服务端会话与历史归属。"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config.db import SessionLocal
from app.config.settings import settings
from app.core.crypto import encrypt
from app.core.oidc import OIDCEndpoints, OIDCError, verify_id_token
from app.core.user_accounts import UserProvisionError, provision_oidc_user
from app.core.user_auth import (
    csrf_cookie_name,
    create_user_session,
    session_cookie_name,
)
from app.core.user_profile import normalize_email, normalize_phone, normalize_username
from app.main import create_app
from app.models.conversation import Conversation
from app.models.enums import RunStatus
from app.models.oauth import OAuthProvider
from app.models.run import Run
from app.models.user import AppUser
from app.models.workflow import Workflow
from tests.conftest import authenticate_admin


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch):
    """测试使用 http 本地客户端，因此关闭 Secure Cookie，并允许显式 IdP URL。"""
    monkeypatch.setattr(settings, "user_session_cookie_secure", False)
    monkeypatch.setattr(settings, "oauth_public_base_url", "http://test")
    monkeypatch.setattr(settings, "oauth_allow_private_networks", True)
    monkeypatch.setattr(settings, "auth_default_tenant", "default")


async def _client() -> httpx.AsyncClient:
    """构造不自动跟随重定向的 ASGI 测试客户端。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
        follow_redirects=False,
    )


async def _create_user(tenant_id: str, username: str) -> AppUser:
    """直接创建一个本地测试用户并返回持久化后的 ORM 对象。"""
    async with SessionLocal() as db:
        user = AppUser(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-user:{tenant_id}:{username}")),
            tenant_id=tenant_id,
            username=username,
            status="active",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def _authenticated_client(user: AppUser) -> tuple[httpx.AsyncClient, str]:
    """为用户创建正式服务端会话，并返回带 Cookie/CSRF 的客户端。"""
    async with SessionLocal() as db:
        token, csrf = await create_user_session(db, user.id)
        await db.commit()
    client = await _client()
    client.cookies.set(session_cookie_name(), token)
    client.cookies.set(csrf_cookie_name(), csrf)
    return client, csrf


async def _create_workflow(code: str) -> Workflow:
    """插入一个已启用 Workflow，供会话创建接口使用。"""
    async with SessionLocal() as db:
        workflow = Workflow(
            code=code,
            name=code,
            definition={},
            enabled=1,
        )
        db.add(workflow)
        await db.commit()
        await db.refresh(workflow)
        return workflow


def _csrf(headers: dict | None = None, token: str | None = None) -> dict:
    """复制 headers 并按需追加 CSRF 请求头。"""
    out = dict(headers or {})
    if token:
        out["X-CSRF-Token"] = token
    return out


def test_profile_normalization():
    """username/email/phone 规范化不依赖旧客户端协议。"""
    assert normalize_username(" Alice.01 ") == "alice.01"
    assert normalize_username("a") is None
    assert normalize_username("张三") is None
    assert normalize_email(" Alice@Example.COM ") == "alice@example.com"
    assert normalize_email("") is None
    assert normalize_phone(" +8613800138000 ") == "+8613800138000"
    assert normalize_phone("13800138000") is None


async def test_full_oidc_login_session_csrf_and_conversation(monkeypatch):
    """完整走 start → callback → me → 创建会话 → logout。"""
    import app.core.oidc as oidc

    oidc._JWKS_CACHE.clear()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key.public_key(), as_dict=True
    )
    public_jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})

    async with SessionLocal() as db:
        provider = OAuthProvider(
            tenant_id="default",
            code="mock",
            display_name="Mock OIDC",
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
    await _create_workflow("oauth_wf")

    async def fake_fetch(url: str) -> dict:
        """返回测试 RSA 公钥 JWKS。"""
        assert url == "https://idp.example/jwks"
        return {"keys": [public_jwk]}

    started: dict[str, str] = {}

    async def fake_post(url: str, data: dict[str, str], *, basic_auth=None) -> dict:
        """模拟 token endpoint，同时核对 PKCE 与客户端认证。"""
        assert url == "https://idp.example/token"
        assert data["code"] == "auth-code"
        assert basic_auth == ("client-1", "secret")
        verifier = data["code_verifier"]
        challenge = started["code_challenge"]
        actual = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert actual == challenge
        claims = {
            "iss": "https://idp.example",
            "aud": "client-1",
            "sub": "subject-a",
            "preferred_username": "Alice",
            "email": "Alice@Example.COM",
            "email_verified": True,
            "phone_number": "+8613800138000",
            "phone_number_verified": True,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "nonce": started["nonce"],
        }
        return {
            "id_token": jwt.encode(
                claims,
                private_key,
                algorithm="RS256",
                headers={"kid": "test-key"},
            )
        }

    monkeypatch.setattr(oidc, "_fetch_json", fake_fetch)
    monkeypatch.setattr(oidc, "_post_form", fake_post)

    async with await _client() as client:
        providers = await client.get("/api/auth/providers")
        assert providers.status_code == 200
        assert providers.json() == [
            {"code": "mock", "display_name": "Mock OIDC", "kind": "oidc"}
        ]

        start = await client.post(
            "/api/auth/oauth/mock/start", json={"return_to": "/"}
        )
        assert start.status_code == 200, start.text
        authorization = urlsplit(start.json()["authorization_url"])
        query = parse_qs(authorization.query)
        started.update({key: values[0] for key, values in query.items()})
        assert started["code_challenge_method"] == "S256"
        assert "openid" in started["scope"].split()
        assert "wa_oauth_state" in client.cookies

        callback = await client.get(
            "/api/auth/oauth/mock/callback",
            params={"code": "auth-code", "state": started["state"]},
        )
        assert callback.status_code == 303, callback.text
        assert callback.headers["location"] == "/"
        assert "wa_oauth_state" not in client.cookies
        assert "wa_session" in client.cookies
        assert "wa_csrf" in client.cookies

        csrf = client.cookies.get(csrf_cookie_name())
        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json() == {
            "username": "alice",
            "phone": "+8613800138000",
            "phone_verified": True,
            "email": "alice@example.com",
            "email_verified": True,
            "status": "active",
        }

        missing_csrf = await client.post(
            "/api/conversations", json={"workflow_code": "oauth_wf"}
        )
        assert missing_csrf.status_code == 403

        created = await client.post(
            "/api/conversations",
            json={
                "workflow_code": "oauth_wf",
                "user_id": "forged-user",
                "session_id": "public-session",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201, created.text
        assert "user_id" not in created.json()

        listed = await client.get("/api/conversations")
        assert [row["session_id"] for row in listed.json()] == ["public-session"]

        logout = await client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": csrf}
        )
        assert logout.status_code == 200
        assert (await client.get("/api/auth/me")).status_code == 401


async def test_conversation_and_run_ownership_returns_404():
    """用户 B 不能枚举或操作用户 A 的资源，跨租户也使用同一规则。"""
    alice = await _create_user("tenant-a", "alice-a")
    bob = await _create_user("tenant-a", "bob-a")
    other = await _create_user("tenant-b", "alice-a")
    workflow = await _create_workflow("owner_wf")
    async with SessionLocal() as db:
        conversation = Conversation(
            session_id="alice-session",
            tenant_id=alice.tenant_id,
            owner_user_id=alice.id,
            workflow_id=workflow.id,
            user_id="legacy-ignored",
        )
        db.add(conversation)
        await db.flush()
        run = Run(
            conversation_id=conversation.id,
            workflow_id=workflow.id,
            status=RunStatus.RUNNING.value,
        )
        db.add(run)
        await db.commit()
        conversation_id = conversation.id
        run_id = run.id

    for stranger in (bob, other):
        client, csrf = await _authenticated_client(stranger)
        try:
            assert (await client.get("/api/conversations")).json() == []
            checks = [
                await client.get(f"/api/conversations/{conversation_id}/messages"),
                await client.get(f"/api/conversations/{conversation_id}/active-run"),
                await client.get(f"/api/runs/{run_id}"),
                await client.get(f"/api/runs/{run_id}/messages"),
                await client.get(f"/api/runs/{run_id}/approval"),
                await client.post(
                    f"/api/runs/{run_id}/cancel",
                    headers=_csrf(token=csrf),
                ),
                await client.post(
                    f"/api/conversations/{conversation_id}/messages",
                    json={"content": "越权"},
                    headers=_csrf(token=csrf),
                ),
                await client.post(
                    "/api/auth/sse-ticket",
                    json={"run_id": run_id},
                    headers=_csrf(token=csrf),
                ),
            ]
            assert [response.status_code for response in checks] == [404] * len(checks)
        finally:
            await client.aclose()

    owner_client, owner_csrf = await _authenticated_client(alice)
    try:
        assert (await owner_client.get(f"/api/runs/{run_id}")).status_code == 200
        ticket = await owner_client.post(
            "/api/auth/sse-ticket",
            json={"run_id": run_id},
            headers=_csrf(token=owner_csrf),
        )
        assert ticket.status_code == 200
    finally:
        await owner_client.aclose()


async def test_state_cannot_be_replayed(monkeypatch):
    """一次性 state 在第一个回调消费后不能再次换取会话。"""
    import app.core.oidc as oidc
    import app.api.routes.auth as auth_routes

    async with SessionLocal() as db:
        provider = OAuthProvider(
            tenant_id="default",
            code="replay",
            display_name="Replay",
            issuer="https://idp.example",
            authorization_endpoint="https://idp.example/authorize",
            token_endpoint="https://idp.example/token",
            jwks_uri="https://idp.example/jwks",
            client_id="client-2",
            enabled=1,
        )
        db.add(provider)
        await db.commit()

    async def fail_network(*args, **kwargs):
        """state 重放路径不应再次访问 provider。"""
        raise AssertionError("重复 state 不应触发网络请求")

    async def fail_exchange(*args, **kwargs):
        """模拟首次回调在消费 state 后遇到的网络失败。"""
        from app.core.oidc import OIDCError

        raise OIDCError("network_disabled")

    async with await _client() as client:
        started = await client.post("/api/auth/oauth/replay/start", json={})
        state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
        binding = client.cookies.get("wa_oauth_state")

        monkeypatch.setattr(auth_routes, "resolve_provider_endpoints", fail_network)
        monkeypatch.setattr(auth_routes, "exchange_code", fail_exchange)
        # 第一次回调在消费 state 后才调用 OIDC；这里让消费成功、网络失败。
        first = await client.get(
            "/api/auth/oauth/replay/callback",
            params={"code": "c", "state": state},
        )
        assert first.status_code == 303
        assert first.headers["location"].startswith("/?auth_error=")

        client.cookies.set("wa_oauth_state", binding)
        second = await client.get(
            "/api/auth/oauth/replay/callback",
            params={"code": "c", "state": state},
        )
        assert second.status_code == 303
        assert "auth_error=state_expired" in second.headers["location"]


async def test_id_token_rejects_wrong_nonce_and_tampering(monkeypatch):
    """真实 JWKS 验签路径必须拒绝 nonce 不符和签名篡改。"""
    import app.core.oidc as oidc

    oidc._JWKS_CACHE.clear()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
        private_key.public_key(), as_dict=True
    )
    public_jwk.update({"kid": "strict-key", "alg": "RS256", "use": "sig"})

    async def fake_fetch(url: str) -> dict:
        """返回严格验签测试使用的 JWKS。"""
        return {"keys": [public_jwk]}

    monkeypatch.setattr(oidc, "_fetch_json", fake_fetch)
    provider = OAuthProvider(
        tenant_id="default",
        code="strict",
        display_name="Strict",
        issuer="https://idp.example",
        client_id="strict-client",
        enabled=1,
    )
    endpoints = OIDCEndpoints(
        issuer="https://idp.example",
        authorization_endpoint="https://idp.example/authorize",
        token_endpoint="https://idp.example/token",
        jwks_uri="https://idp.example/jwks",
    )
    claims = {
        "iss": "https://idp.example",
        "aud": "strict-client",
        "sub": "subject-strict",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "nonce": "correct-nonce",
    }
    token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "strict-key"},
    )
    expected_hash = hashlib.sha256(b"correct-nonce").hexdigest()
    assert (await verify_id_token(
        provider, endpoints, token, expected_nonce_hash=expected_hash
    ))["sub"] == "subject-strict"
    with pytest.raises(OIDCError, match="nonce_mismatch"):
        await verify_id_token(
            provider,
            endpoints,
            token,
            expected_nonce_hash=hashlib.sha256(b"wrong").hexdigest(),
        )
    with pytest.raises(OIDCError, match="id_token_invalid"):
        await verify_id_token(
            provider,
            endpoints,
            token[:-2] + ("aa" if not token.endswith("aa") else "bb"),
            expected_nonce_hash=expected_hash,
        )


async def test_identity_binding_does_not_merge_profile_conflicts():
    """首次建号遇到 username/email 冲突必须拒绝，同 subject 则稳定复用。"""
    tag = uuid.uuid4().hex[:8]
    existing_username = f"alice-{tag}"
    existing_email = f"used-{tag}@example.com"
    provider_code = f"accounts-{tag}"
    new_username = f"new-{tag}"
    async with SessionLocal() as db:
        existing = AppUser(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"conflict-existing:{tag}")),
            tenant_id="default",
            username=existing_username,
            email=existing_email,
            status="active",
        )
        provider = OAuthProvider(
            tenant_id="default",
            code=provider_code,
            display_name="Accounts",
            issuer="https://accounts.example",
            client_id="accounts-client",
            auto_create=1,
            enabled=1,
        )
        db.add(existing)
        db.add(provider)
        await db.commit()
        await db.refresh(provider)

        with pytest.raises(UserProvisionError, match="username_conflict"):
            await provision_oidc_user(
                db,
                provider,
                {
                    "sub": "subject-conflict-name",
                    "preferred_username": existing_username,
                },
            )
        with pytest.raises(UserProvisionError, match="email_conflict"):
            await provision_oidc_user(
                db,
                provider,
                {
                    "sub": "subject-conflict-email",
                    "preferred_username": f"bob-{tag}",
                    "email": existing_email.upper(),
                },
            )

        first = await provision_oidc_user(
            db,
            provider,
            {"sub": f"subject-new-{tag}", "preferred_username": new_username},
        )
        second = await provision_oidc_user(
            db,
            provider,
            {
                "sub": f"subject-new-{tag}",
                "preferred_username": f"renamed-{tag}",
            },
        )
        assert first.user.id == second.user.id
        assert second.user.username == new_username


async def test_admin_can_configure_provider_without_secret_echo():
    """供应商管理员接口只回显 secret 是否已配置，落库必须是密文。"""
    code = f"admin-{uuid.uuid4().hex[:8]}"
    async with await _client() as client:
        await authenticate_admin(client)
        created = await client.post(
            "/api/oauth-providers",
            json={
                "code": code,
                "display_name": "Admin OIDC",
                "issuer": "https://login.example",
                "authorization_endpoint": "https://login.example/authorize",
                "token_endpoint": "https://login.example/token",
                "jwks_uri": "https://login.example/jwks",
                "client_id": "admin-client",
                "client_secret": "super-secret-value",
                "scopes": ["openid", "profile"],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["has_client_secret"] is True
        assert "client_secret" not in body
        assert "super-secret-value" not in created.text

        async with SessionLocal() as db:
            from app.repo.oauth_provider_repo import oauth_provider_repo

            row = await oauth_provider_repo.get(db, code, "default")
            assert row is not None
            assert row.client_secret_cipher.startswith("fernet:")


def test_secure_cookie_contract():
    """Secure 模式使用 __Host- Cookie，并带 HttpOnly/SameSite。"""
    from fastapi import Response

    from app.core.user_auth import set_user_cookies

    old = settings.user_session_cookie_secure
    settings.user_session_cookie_secure = True
    try:
        response = Response()
        set_user_cookies(response, "session", "csrf")
        cookies = response.headers.getlist("set-cookie")
        assert any("__Host-wa_session=session" in item for item in cookies)
        assert any("__Host-wa_csrf=csrf" in item for item in cookies)
        assert all("Secure" in item for item in cookies)
        assert all("Path=/" in item for item in cookies)
        assert any("HttpOnly" in item and "SameSite=lax" in item for item in cookies)
    finally:
        settings.user_session_cookie_secure = old
