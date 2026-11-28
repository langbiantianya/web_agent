"""管理面 JSON API —— 平台管理员、外观偏好、工具审计与撤销（V4.10）。

锁住四件事：**非 owner 看不到平台管理员页**、**外观取值不合法要被拒**、
**删除后能撤销**、以及**审计查询按租户过滤**。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import httpx
import sqlalchemy as sa

from app.models.admin_control import SuperAdminAccount, SuperAdminSession
from services.admin_web.accounts import (
    create_super_admin_session,
    ensure_owner_super_admin,
)
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


def _owner_statement():
    """取 owner 账号的查询。"""
    return sa.select(SuperAdminAccount).where(
        SuperAdminAccount.owner_slot == "superAdmin"
    )


async def _drop_owner() -> None:
    """删掉测试期间创建的 owner 账号。

    owner 全局只有一个（数据库上有唯一约束），而且后续用例断言「首次安装
    才生成 owner」。测试留下的 owner 会让那些用例误判，所以这里主动清理。
    """
    async with AdminSessionLocal() as db:
        owner = await db.scalar(_owner_statement())
        if owner is None:
            return
        await db.execute(
            sa.delete(SuperAdminSession).where(
                SuperAdminSession.account_id == owner.id
            )
        )
        await db.delete(owner)
        await db.commit()


@asynccontextmanager
async def _client(*, owner: bool):
    """建立带会话的 ASGI 客户端；``owner=True`` 时用 owner 账号登录。

    owner 是全局唯一的，测试自己造一个并在退出时删掉——否则后面的用例
    会因为「owner 已存在」而误判。
    """
    from services.admin_web.accounts import create_super_admin_account
    from services.admin_web.app import app as admin_asgi_app

    tag = uuid.uuid4().hex[:8]
    created_owner = False
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        if owner:
            await ensure_owner_super_admin(db)
            account = await db.scalar(_owner_statement())
            created_owner = True
        else:
            account, _ = await create_super_admin_account(
                db,
                username=f"api_{tag}",
                email=f"api-{tag}@example.com",
                phone=None,
                owner=False,
                password_sender=_password_sender(),
            )
        token, csrf = await create_super_admin_session(db, account)
        await db.commit()
    # 通知渠道在测试里是关着的；换成「总是送达」的测试发送器，
    # 否则创建账号会走到「通知失败」分支。用完要**还原**——应用上的这个
    # 发送器是全局状态，留在测试里会让后面的用例收到「假成功」。
    previous_sender = admin_asgi_app.state.admin_password_sender
    admin_asgi_app.state.admin_password_sender = _password_sender()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    try:
        yield client
    finally:
        await client.aclose()
        admin_asgi_app.state.admin_password_sender = previous_sender
        if created_owner:
            await _drop_owner()


def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    """取 CSRF 头。"""
    return {"x-csrf-token": client.cookies.get(super_csrf_cookie_name()) or ""}


async def test_preferences_read_and_save() -> None:
    """偏好可读可存，非法取值要被拒。"""
    async with _client(owner=False) as client:
        listed = await client.get("/admin/api/preferences")
        assert listed.status_code == 200, listed.text
        payload = listed.json()
        assert payload["theme"] in payload["options"]["themes"]

        saved = await client.put(
            "/admin/api/preferences",
            headers=_headers(client),
            json={"theme": "material", "colorMode": "dark", "density": "compact"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["theme"] == "material"
        assert saved.json()["colorMode"] == "dark"

        invalid = await client.put(
            "/admin/api/preferences",
            headers=_headers(client),
            json={"theme": "nope", "colorMode": "dark", "density": "compact"},
        )
        assert invalid.status_code == 400, invalid.text
        assert invalid.json()["code"] == "invalid_input"


async def test_audit_list_is_scoped() -> None:
    """审计列表可读，且按租户过滤。"""
    async with _client(owner=False) as client:
        listed = await client.get("/admin/api/audit")
        assert listed.status_code == 200, listed.text
        payload = listed.json()
        assert payload["items"] == []
        assert payload["filters"]["ok"] == ""
        assert "builtin" in payload["sources"]


async def test_platform_admins_require_owner() -> None:
    """非 owner 访问平台管理员接口一律 404。"""
    async with _client(owner=False) as client:
        listed = await client.get("/admin/api/platform-admins")
        assert listed.status_code == 404, listed.text
        assert listed.json()["code"] == "not_found"


async def test_platform_admin_create_delete_and_undo() -> None:
    """owner 能建、能删，删完还能撤销回来。"""
    async with _client(owner=True) as client:
        tag = uuid.uuid4().hex[:8]
        listed = await client.get("/admin/api/platform-admins")
        assert listed.status_code == 200, listed.text
        assert any(
            item["username"] == "superAdmin" for item in listed.json()["items"]
        )

        created = await client.post(
            "/admin/api/platform-admins",
            headers=_headers(client),
            json={
                "username": f"helper_{tag}",
                "email": f"helper-{tag}@example.com",
                "phone": "",
            },
        )
        assert created.status_code == 201, created.text
        account_id = created.json()["item"]["id"]

        removed = await client.delete(
            f"/admin/api/platform-admins/{account_id}", headers=_headers(client)
        )
        assert removed.status_code == 200, removed.text
        undo_id = removed.json()["undoId"]
        assert undo_id

        undone = await client.post(
            f"/admin/api/undo/{undo_id}", headers=_headers(client), json={}
        )
        assert undone.status_code == 200, undone.text
        assert undone.json()["item"]["resourceType"] == "super_admin_account"

        again = await client.get("/admin/api/platform-admins")
        assert any(item["id"] == account_id for item in again.json()["items"])


async def test_platform_admin_owner_cannot_be_deleted() -> None:
    """owner 账号受保护，删不掉。"""
    async with _client(owner=True) as client:
        listed = await client.get("/admin/api/platform-admins")
        owner_item = next(
            item
            for item in listed.json()["items"]
            if item["username"] == "superAdmin"
        )
        removed = await client.delete(
            f"/admin/api/platform-admins/{owner_item['id']}",
            headers=_headers(client),
        )
        assert removed.status_code == 409, removed.text
        assert removed.json()["code"] == "in_use"
