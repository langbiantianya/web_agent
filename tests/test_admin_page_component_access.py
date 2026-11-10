"""页面与组件权限矩阵的保存、覆盖和重置测试。"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.models.admin_control import (
    AdminComponentPermission,
    AdminPagePermission,
)
from services.admin_web.db import AdminSessionLocal
from tests.test_admin_complete import _logged_in_client


@pytest.mark.asyncio
async def test_page_permissions_render_matrix_and_tenant_reset_control():
    """页面权限使用复选框矩阵，并明确提供租户覆盖重置入口。"""
    client, csrf = await _logged_in_client()
    tenant_id = "matrix-page-tenant"
    try:
        created = await client.post(
            "/admin/access/pages/preferences/permissions",
            data={
                "csrf_token": csrf,
                "tenant_id": tenant_id,
                "operation": "save",
                "permissions": ["admin.read", "workflow.read"],
            },
        )
        assert created.status_code == 303
        assert "notice=updated" in created.headers["location"]
        async with AdminSessionLocal() as db:
            saved_rows = list(
                await db.scalars(
                    sa.select(AdminPagePermission).where(
                        AdminPagePermission.page_code == "preferences",
                        AdminPagePermission.tenant_id == tenant_id,
                    )
                )
            )
            assert saved_rows
        page = await client.get("/admin/access/pages")
        assert page.status_code == 200
        text = page.text
        assert 'type="checkbox" name="permissions"' in text
        assert "Recommended" in text
        assert tenant_id in text
        last_tenant = text.rfind(tenant_id)
        assert "Reset to global" in text, text[
            max(0, last_tenant - 1200):last_tenant + 2400
        ]
        assert 'value="reset"' in text
        async with AdminSessionLocal() as db:
            rows = list(
                await db.scalars(
                    sa.select(AdminPagePermission).where(
                        AdminPagePermission.page_code == "preferences",
                        AdminPagePermission.tenant_id == tenant_id,
                    )
                )
            )
            assert {(row.object_name, row.action_name) for row in rows} == {
                ("admin", "read"),
                ("workflow", "read"),
            }
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_page_override_reset_falls_back_to_global_and_critical_guard_works():
    """重置只删除租户覆盖；关键页面的全局绑定不能被清空。"""
    client, csrf = await _logged_in_client()
    tenant_id = "matrix-reset-tenant"
    try:
        await client.post(
            "/admin/access/pages/audit/permissions",
            data={
                "csrf_token": csrf,
                "tenant_id": tenant_id,
                "operation": "save",
                "permissions": ["workflow.read"],
            },
        )
        reset = await client.post(
            "/admin/access/pages/audit/permissions",
            data={
                "csrf_token": csrf,
                "tenant_id": tenant_id,
                "operation": "reset",
            },
        )
        assert reset.status_code == 303
        async with AdminSessionLocal() as db:
            assert await db.scalar(
                sa.select(AdminPagePermission.id).where(
                    AdminPagePermission.page_code == "audit",
                    AdminPagePermission.tenant_id == tenant_id,
                )
            ) is None
            assert await db.scalar(
                sa.select(AdminPagePermission.id).where(
                    AdminPagePermission.page_code == "audit",
                    AdminPagePermission.tenant_id.is_(None),
                )
            ) is not None
        critical = await client.post(
            "/admin/access/pages/dashboard/permissions",
            data={
                "csrf_token": csrf,
                "tenant_id": "",
                "operation": "save",
            },
        )
        assert critical.status_code == 400
        assert "Critical system pages" in critical.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_component_permissions_save_empty_tenant_is_rejected_and_reset_works():
    """组件覆盖不能用空集合保存；显式重置后可回退全局。"""
    client, csrf = await _logged_in_client()
    tenant_id = "matrix-component-tenant"
    action = "/admin/access/components/preferences/theme_form/permissions"
    try:
        empty = await client.post(
            action,
            data={
                "csrf_token": csrf,
                "tenant_id": tenant_id,
                "operation": "save",
            },
        )
        assert empty.status_code == 303
        assert "error=invalid" in empty.headers["location"]
        saved = await client.post(
            action,
            data={
                "csrf_token": csrf,
                "tenant_id": tenant_id,
                "operation": "save",
                "permissions": ["admin.read", "agent.read"],
            },
        )
        assert saved.status_code == 303
        page = await client.get("/admin/access/components")
        assert "theme_form" in page.text
        assert "Reset to global" in page.text
        async with AdminSessionLocal() as db:
            rows = list(
                await db.scalars(
                    sa.select(AdminComponentPermission).where(
                        AdminComponentPermission.page_code == "preferences",
                        AdminComponentPermission.component_code == "theme_form",
                        AdminComponentPermission.tenant_id == tenant_id,
                    )
                )
            )
            assert len(rows) == 2
        reset = await client.post(
            action,
            data={
                "csrf_token": csrf,
                "tenant_id": tenant_id,
                "operation": "reset",
            },
        )
        assert reset.status_code == 303
        async with AdminSessionLocal() as db:
            assert await db.scalar(
                sa.select(AdminComponentPermission.id).where(
                    AdminComponentPermission.page_code == "preferences",
                    AdminComponentPermission.component_code == "theme_form",
                    AdminComponentPermission.tenant_id == tenant_id,
                )
            ) is None
    finally:
        await client.aclose()
