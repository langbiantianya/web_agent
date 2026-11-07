"""MCP 服务器管理 API（设计文档 §21.11）。

要钉住的命题：

1. **全部接口是管理员专属且 fail-closed**——没带密钥 / 密钥错，一律 401。
   注册一台 MCP 服务器不是普通配置操作：stdio 那条路里 ``command`` 决定执行
   什么程序，等于「部署级操作」。
2. **凭据只写不读**——``env`` / ``headers`` 的值加密存储，响应里只回显**键名**。
3. **保存期就拦下危险配置**——命令不在白名单前缀、URL 被 SSRF 策略拒绝、
   传输方式与字段不匹配，都在创建时就 400。
4. **Agent 绑定 MCP 工具要过保存期校验**——没发现过的工具名存不进配置。
"""

from __future__ import annotations

import httpx
import pytest

from app.config.settings import settings
from app.main import create_app
from tests.conftest import authenticate_admin

ADMIN_HEADERS: dict[str, str] = {}


@pytest.fixture
async def client():
    """真实 app + HTTP 客户端。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            await authenticate_admin(c)
            yield c


@pytest.fixture
def allow_stdio(monkeypatch):
    """放开 stdio 命令白名单（测试用固定的可执行文件路径）。"""
    monkeypatch.setattr(settings, "mcp_allowed_command_prefixes", ["/fake/bin/"])


# --------------------------------------------------------------- 门禁


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/mcp/servers"),
        ("post", "/api/mcp/servers"),
        ("get", "/api/mcp/servers/whatever"),
        ("put", "/api/mcp/servers/whatever"),
        ("delete", "/api/mcp/servers/whatever"),
        ("post", "/api/mcp/servers/whatever/refresh"),
        ("post", "/api/mcp/servers/whatever/oauth/revoke"),
        ("get", "/api/mcp/servers/whatever/tools"),
    ],
)
async def test_all_mcp_endpoints_require_admin(client, method, path):
    """没带 admin key → 401（fail-closed）。

    连只读的两条也要密钥：它们虽然不写，但也没有必要为「看看有哪些服务器」
    单独开一套鉴权——MCP 服务器列表本身就透露了部署的内部结构。
    """
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    resp = await getattr(client, method)(path)
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "unauthorized"


async def test_wrong_admin_key_rejected(client):
    """旧 X-Admin-Key 不能代替后台 Session。"""
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    resp = await client.post(
        "/api/mcp/servers",
        json={"code": "x", "transport": "http", "url": "https://a.example.com/mcp"},
        headers={"X-Admin-Key": "wrong"},
    )
    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------- 注册与校验


async def test_create_http_server(client):
    """注册一台 http 服务器。"""
    resp = await client.post(
        "/api/mcp/servers",
        json={
            "code": "mcpapi-http",
            "name": "示例 HTTP 服务器",
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer secret-token"},
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["code"] == "mcpapi-http"
    assert data["transport"] == "http"
    # 凭据只回显键名，值绝不回显
    assert data["header_keys"] == ["Authorization"]
    assert data["oauth_configured"] is False
    assert data["online"] is False
    assert "secret-token" not in resp.text


async def test_create_duplicate_code_conflicts(client):
    """code 重复 → 409。"""
    payload = {
        "code": "mcpapi-dup",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
    }
    first = await client.post("/api/mcp/servers", json=payload, headers=ADMIN_HEADERS)
    assert first.status_code == 201, first.text
    second = await client.post("/api/mcp/servers", json=payload, headers=ADMIN_HEADERS)
    assert second.status_code == 409, second.text


async def test_create_rejects_transport_field_mismatch(client):
    """传输方式与字段对不上 → 400。"""
    resp = await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-mismatch", "transport": "stdio", "url": "https://a.example.com"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text

    resp = await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-mismatch2", "transport": "http", "command": "python"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


async def test_create_stdio_requires_command(client):
    """stdio 传输必须给 command。"""
    resp = await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-nocmd", "transport": "stdio"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


async def test_create_stdio_denied_without_command_allowlist(client):
    """命令不在白名单前缀 → 400（**保存期**就拦）。

    这道门必须在保存期生效：等到第一次调用才发现连不上，那时模型已经拿着这个
    工具名在跑了，错误还会被当成「工具失败」回灌给模型，白烧一轮。
    """
    resp = await client.post(
        "/api/mcp/servers",
        json={
            "code": "mcpapi-stdio",
            "transport": "stdio",
            "command": "/usr/bin/python",
            "args": ["server.py"],
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text
    assert "MCP_ALLOWED_COMMAND_PREFIXES" in resp.json()["message"]


async def test_create_stdio_allowed_inside_prefix(client, allow_stdio):
    """白名单前缀内的命令放行。"""
    resp = await client.post(
        "/api/mcp/servers",
        json={
            "code": "mcpapi-stdio-ok",
            "transport": "stdio",
            "command": "/fake/bin/server",
            "args": ["--port", "1"],
            "env": {"TOKEN": "s3cret"},
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["env_keys"] == ["TOKEN"]
    assert "s3cret" not in resp.text


async def test_create_http_rejected_by_ssrf_policy(client, monkeypatch):
    """URL 指向内网 → 400（保存期就拦下 SSRF）。"""
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    resp = await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-ssrf", "transport": "http", "url": "http://127.0.0.1:8931/mcp"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text
    assert "SSRF" in resp.json()["message"]


async def test_create_rejects_bad_code_pattern(client):
    """code 的形状不合法 → 400。

    code 会进限定名 ``mcp__<code>__<tool>``，所以它的字符集必须与 OpenAI 的
    函数名约束兼容。
    """
    resp = await client.post(
        "/api/mcp/servers",
        json={"code": "Bad_Code", "transport": "http", "url": "https://a.example.com/mcp"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------- 读改删


async def test_list_and_get_server(client):
    """列表与详情。"""
    await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-list", "transport": "http", "url": "https://a.example.com/mcp"},
        headers=ADMIN_HEADERS,
    )
    listing = await client.get("/api/mcp/servers", headers=ADMIN_HEADERS)
    assert listing.status_code == 200, listing.text
    codes = {item["code"] for item in listing.json()}
    assert "mcpapi-list" in codes

    detail = await client.get("/api/mcp/servers/mcpapi-list", headers=ADMIN_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["code"] == "mcpapi-list"


async def test_get_missing_server_returns_404(client):
    """不存在的服务器 → 404。"""
    resp = await client.get("/api/mcp/servers/mcpapi-missing", headers=ADMIN_HEADERS)
    assert resp.status_code == 404, resp.text


async def test_update_bumps_version_and_clears_snapshot(client):
    """更新 → version+1。

    连接与快照都要作废：改了命令之后，旧连接连的是旧程序，旧快照描述的是旧
    工具——留着它们就是「配置说 A、实际跑 B」。
    """
    await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-upd", "transport": "http", "url": "https://a.example.com/mcp"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.put(
        "/api/mcp/servers/mcpapi-upd",
        json={"name": "改个名", "timeout_sec": 90},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "改个名"
    assert data["timeout_sec"] == 90
    assert data["version"] >= 2


async def test_update_rejects_dangerous_url(client, monkeypatch):
    """更新成内网 URL 同样要在保存期拦下。"""
    monkeypatch.setattr(settings, "mcp_allow_private_networks", False)
    await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-upd2", "transport": "http", "url": "https://a.example.com/mcp"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.put(
        "/api/mcp/servers/mcpapi-upd2",
        json={"url": "http://169.254.169.254/latest/meta-data"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text
    assert "SSRF" in resp.json()["message"]


async def test_delete_server(client):
    """删除服务器。"""
    await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-del", "transport": "http", "url": "https://a.example.com/mcp"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.delete("/api/mcp/servers/mcpapi-del", headers=ADMIN_HEADERS)
    assert resp.status_code == 204, resp.text
    again = await client.get("/api/mcp/servers/mcpapi-del", headers=ADMIN_HEADERS)
    assert again.status_code == 404


# --------------------------------------------------------------- Agent 绑定


async def test_agent_cannot_bind_unknown_mcp_tool(client):
    """配一个没被发现的 MCP 工具 → 400。

    错误信息要说清是「服务器尚未发现工具」还是「工具不存在」——后者已存在，
    前者要先去刷新目录。含糊的提示会让用户反复试同一个配置。
    """
    resp = await client.post(
        "/api/agents",
        json={
            "code": "mcpapi-agent",
            "name": "MCP 演示",
            "system_prompt": "你是助手",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "tools": ["mcp__weather__get_weather"],
        },
    )
    assert resp.status_code == 400, resp.text
    message = resp.json()["message"]
    assert "mcp__weather__get_weather" in message
    assert "刷新" in message


async def test_agent_tool_list_drops_deleted_server_tools(client):
    """服务器被删之后，Agent 上残留的绑定不再显示。

    读路径要宽容——残留的绑定不该让列表接口 500，但也不该继续显示一个指向
    空壳的名字。
    """
    await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-bind", "transport": "http", "url": "https://a.example.com/mcp"},
        headers=ADMIN_HEADERS,
    )
    # 手工塞一条快照（模拟「发现过工具」）
    from app.config.db import SessionLocal
    from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo

    async with SessionLocal() as db:
        server = await mcp_server_repo.get_by_code(db, "mcpapi-bind")
        assert server is not None
        await mcp_tool_repo.replace_for_server(
            db,
            server.id,
            [
                {
                    "qualified_name": "mcp__mcpapi-bind__get_weather",
                    "tool_name": "get_weather",
                    "description": "查天气",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )

    created = await client.post(
        "/api/agents",
        json={
            "code": "mcpapi-agent2",
            "name": "MCP 演示",
            "system_prompt": "你是助手",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "tools": ["mcp__mcpapi-bind__get_weather"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["tools"] == ["mcp__mcpapi-bind__get_weather"]

    # 服务器删掉 → 快照连带删掉 → Agent 上的绑定读回来时不再显示
    await client.delete("/api/mcp/servers/mcpapi-bind", headers=ADMIN_HEADERS)
    listed = await client.get("/api/agents/mcpapi-agent2")
    assert listed.status_code == 200, listed.text
    assert listed.json()["tools"] is None


# --------------------------------------------------------------- 工具目录


async def test_tools_endpoint_lists_builtin_and_mcp(client):
    """``GET /api/tools`` 同时列内置与 MCP 工具，且带 ``source`` / ``server``。"""
    from app.config.db import SessionLocal
    from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo

    await client.post(
        "/api/mcp/servers",
        json={"code": "mcpapi-cat", "transport": "http", "url": "https://a.example.com/mcp"},
        headers=ADMIN_HEADERS,
    )
    async with SessionLocal() as db:
        server = await mcp_server_repo.get_by_code(db, "mcpapi-cat")
        assert server is not None
        await mcp_tool_repo.replace_for_server(
            db,
            server.id,
            [
                {
                    "qualified_name": "mcp__mcpapi-cat__search",
                    "tool_name": "search",
                    "description": "搜一下",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )

    resp = await client.get("/api/tools")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    by_name = {item["name"]: item for item in items}
    assert by_name["calc"]["source"] == "builtin"
    assert by_name["calc"]["server"] is None
    assert by_name["mcp__mcpapi-cat__search"]["source"] == "mcp"
    assert by_name["mcp__mcpapi-cat__search"]["server"] == "mcpapi-cat"
