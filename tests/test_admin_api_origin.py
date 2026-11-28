"""管理面 JSON API —— 同源校验策略（V4.10）。

网关（admin-ui）必须知道管理端当前是**白名单模式**还是 **Host 模式**，
否则转发给管理端的 Origin 改写方向会反：白名单模式下改写成内部地址会被
拒，Host 模式下原样转发浏览器地址也会被拒。两种模式下都会把写请求
全部拒掉——登录直接失败。
"""

from __future__ import annotations

import httpx

from services.admin_web.app import app as admin_asgi_app


async def test_origin_policy_reports_mode_and_origins() -> None:
    """策略端点不需要登录，返回模式与白名单。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/admin/api/origin-policy")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["mode"] in {"allowlist", "host"}
        # 两种模式是互斥的：Host 模式没有白名单。
        if payload["mode"] == "host":
            assert payload["origins"] == []
        for origin in payload["origins"]:
            assert origin == origin.rstrip("/")
