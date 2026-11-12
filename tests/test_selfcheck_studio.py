"""自检接口里的 studio（脚本执行服务）判定。

studio 是独立容器：连不上、有没有脚本可跑，这两件事必须**如实**报出来。
把它说成「就绪」，用户就会以为技能脚本能跑——实际上一调就失败。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.api.routes import selfcheck as selfcheck_route
from app.config.db import SessionLocal
from app.core.studio import StudioHealth, StudioProfile
from app.main import create_app
from app.repo.skill_repo import skill_repo
from tests.conftest import authenticate_client


@pytest.fixture
async def client():
    """真实 app + HTTP 客户端（带终端用户会话）。"""
    async with httpx.AsyncClient(
        base_url="http://testserver", transport=httpx.ASGITransport(app=create_app())
    ) as c:
        await authenticate_client(c)
        yield c


def _health(ready: bool = True) -> StudioHealth:
    """造一份 studio 健康信息。

    Args:
        ready: studio 是不是就绪。

    Returns:
        健康信息。
    """
    return StudioHealth(
        ready=ready,
        version="2.0.0",
        languages=("bash", "node", "python"),
        max_concurrency=4,
        profile="fast",
        profiles=(
            StudioProfile(
                name="fast",
                ready=True,
                max_concurrency=4,
                queue_depth=0,
                detail="",
            ),
        ),
    )


async def _seed_skill_with_script(code: str) -> None:
    """建一个带 scripts/ 资源的技能。

    Args:
        code: 技能 code。
    """
    async with SessionLocal() as db:
        row = await skill_repo.get_by_code(db, code, tenant_id="default")
        if row is None:
            row = await skill_repo.create(
                db,
                code=code,
                name=code,
                body="技能正文",
                origin="manual",
                content_hash="h",
                enabled=1,
                status="published",
            )
        await skill_repo.put_resource(
            db,
            row.id,
            "scripts/e2e_sum.py",
            b"print('E2E_STUDIO_SUM:7')\n",
            "text/x-python",
            1,
        )
        await db.commit()


@pytest.mark.asyncio
async def test_studio_not_configured_is_warn(client: httpx.AsyncClient) -> None:
    """没配 studio 地址时是**提醒**不是失败——部署本来就可以不启用脚本执行。"""
    with patch.object(selfcheck_route.settings, "studio_exec_addr", ""):
        with patch.object(selfcheck_route, "get_studio_client", return_value=None):
            resp = await client.get("/api/selfcheck")
    assert resp.status_code == 200
    report = resp.json()
    stages = {s["key"]: s for s in report["studio_stages"]}
    assert stages["studio"]["status"] == "warn"
    assert report["studio"]["ready"] is False
    assert report["studio"]["configured"] is False


@pytest.mark.asyncio
async def test_studio_unreachable_is_fail(client: httpx.AsyncClient) -> None:
    """配了地址但连不上——这是失败：部署不完整。"""
    broken = AsyncMock()
    broken.health.side_effect = RuntimeError("连不上")
    with patch.object(selfcheck_route.settings, "studio_exec_addr", "studio:9000"):
        with patch.object(selfcheck_route, "get_studio_client", return_value=broken):
            resp = await client.get("/api/selfcheck")
    report = resp.json()
    stages = {s["key"]: s for s in report["studio_stages"]}
    assert stages["studio"]["status"] == "fail"


@pytest.mark.asyncio
async def test_studio_ready_lists_script_skills(client: httpx.AsyncClient) -> None:
    """studio 就绪时，脚本资源也要列出来——有脚本才谈得上「跑得起来」。"""
    await _seed_skill_with_script("sc-studio-skill")
    healthy = AsyncMock()
    healthy.health.return_value = _health()
    with patch.object(selfcheck_route.settings, "studio_exec_addr", "studio:9000"):
        with patch.object(selfcheck_route, "get_studio_client", return_value=healthy):
            resp = await client.get("/api/selfcheck")
    report = resp.json()
    stages = {s["key"]: s for s in report["studio_stages"]}
    assert stages["studio"]["status"] == "pass"
    assert stages["studio:scripts"]["status"] == "pass"
    assert report["studio"]["ready"] is True
    assert report["studio"]["version"] == "2.0.0"
    codes = [s["code"] for s in report["studio"]["script_skills"]]
    assert "sc-studio-skill" in codes
    scripts = next(
        s["scripts"] for s in report["studio"]["script_skills"]
        if s["code"] == "sc-studio-skill"
    )
    assert scripts == ["scripts/e2e_sum.py"]
