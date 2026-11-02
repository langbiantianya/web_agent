"""启动期的 studio 检查（设计文档 §22.8.6）。

要钉住的口径：

| 情形 | 结果 |
| --- | --- |
| 地址为空 | 未启用——这不是「不可达」，是「压根没配」 |
| 地址有、连上了 | 启用，``skill_exec`` 注册 |
| 地址有、连不上、``required=true`` | **启动失败** |
| 地址有、连不上、``required=false`` | 未启用，``skill_exec`` 不注册，服务正常启动 |

第四条是 §22.13 的验收项：``STUDIO_EXEC_REQUIRED=false`` 且 studio 不可达时，
服务要正常启动，但 ``skill_exec`` 不注册、且保存期拒绝把它加进工具白名单。
"""

from __future__ import annotations

import httpx
import pytest

from app.config.settings import settings
from app.core import studio as studio_mod
from app.core.studio import StudioStartupError, StudioUnavailableError, init_studio
from app.core.tools import get_tool, set_optional_tools_enabled, tool_names


class _FakeClient:
    """假的 studio 客户端。"""

    def __init__(self, error: Exception | None = None, ready: bool = True) -> None:
        """记住探活该返回什么。"""
        self.error = error
        self.health_info = studio_mod.StudioHealth(
            ready=ready, version="1.0.0", languages=("python",), max_concurrency=4,
            profile="fast",
        )
        self.addr = "studio:9000"

    async def health(self, timeout: float = 5.0):
        """假的探活。"""
        if self.error is not None:
            raise self.error
        return self.health_info

    async def close(self) -> None:
        """假的关闭。"""


@pytest.fixture(autouse=True)
def _reset_registry():
    """每个用例后把可选工具恢复成「未注册」。"""
    yield
    set_optional_tools_enabled(False)


@pytest.fixture
def fake_client(monkeypatch):
    """把 configure_studio 换成假客户端。"""
    holder: dict[str, _FakeClient] = {}

    def _install(client: _FakeClient) -> _FakeClient:
        """装上假客户端并返回它，便于用例链式取用。"""
        holder["client"] = client
        monkeypatch.setattr(studio_mod, "configure_studio", lambda _addr: client)
        return client

    return _install


async def test_not_configured_disables_skill_exec(monkeypatch):
    """地址为空 → 未启用，不注册工具。"""
    monkeypatch.setattr(settings, "studio_exec_addr", "")
    monkeypatch.setattr(settings, "studio_exec_required", True)
    assert await init_studio() is False
    assert get_tool("skill_exec") is None


async def test_reachable_studio_enables_skill_exec(monkeypatch, fake_client):
    """连上了 → 启用。"""
    fake_client(_FakeClient())
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    monkeypatch.setattr(settings, "studio_exec_required", True)
    assert await init_studio() is True


async def test_unreachable_studio_fails_startup_when_required(monkeypatch, fake_client):
    """连不上且 required=true → 抛错（服务据此启动失败）。"""
    fake_client(_FakeClient(error=StudioUnavailableError("拒绝连接")))
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    monkeypatch.setattr(settings, "studio_exec_required", True)
    with pytest.raises(StudioStartupError) as ei:
        await init_studio()
    assert "studio:9000" in str(ei.value)


async def test_unreachable_studio_is_ignored_when_not_required(
    monkeypatch, fake_client
):
    """连不上且 required=false → 只记警告，不抛。"""
    fake_client(_FakeClient(error=StudioUnavailableError("拒绝连接")))
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    monkeypatch.setattr(settings, "studio_exec_required", False)
    assert await init_studio() is False


async def test_not_ready_studio_is_treated_as_unavailable(monkeypatch, fake_client):
    """studio 起来了但 ``ready=false`` → 当作不可用。

    ready=false 的意思是「现在不接流量」（队列满之类），跟连不上一样不能拿来
    执行脚本。
    """
    fake_client(_FakeClient(ready=False))
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    monkeypatch.setattr(settings, "studio_exec_required", True)
    with pytest.raises(StudioStartupError):
        await init_studio()


async def test_unavailable_studio_does_not_register_tool(monkeypatch, fake_client):
    """不可用 → ``skill_exec`` 不注册，也就不出现在工具目录里。"""
    fake_client(_FakeClient(error=StudioUnavailableError("拒绝连接")))
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    monkeypatch.setattr(settings, "studio_exec_required", False)
    assert await init_studio() is False
    set_optional_tools_enabled(False)
    assert "skill_exec" not in tool_names()


# --------------------------------------------------------------- 应用级


async def test_app_startup_fails_when_studio_unreachable(monkeypatch, fake_client):
    """studio 连不上且 required=true → **应用启动失败**。

    §22.8.6：studio 是部署的一个组成部分，它挂了说明部署不完整，让问题在部署
    时暴露，而不是在用户的一次 run 里暴露。
    """
    from app.main import create_app

    fake_client(_FakeClient(error=StudioUnavailableError("拒绝连接")))
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    monkeypatch.setattr(settings, "studio_exec_required", True)

    app = create_app()
    with pytest.raises(StudioStartupError):
        async with app.router.lifespan_context(app):
            pass


async def test_app_starts_when_studio_not_required(monkeypatch, fake_client):
    """required=false 时服务正常启动，只是没有脚本执行能力。"""
    from app.main import create_app

    fake_client(_FakeClient(error=StudioUnavailableError("拒绝连接")))
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    monkeypatch.setattr(settings, "studio_exec_required", False)

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            resp = await c.get("/api/tools")
            assert resp.status_code == 200, resp.text
            names = [item["name"] for item in resp.json()]
            assert "skill_exec" not in names
