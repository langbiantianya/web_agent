"""健康检查。"""

from __future__ import annotations

import sqlalchemy as sa

from fastapi import APIRouter

from app import __version__
from app.api.deps import DbSession
from app.config.settings import settings
from app.core.run_hub import get_hub
from app.schemas.common import HealthOut

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthOut)
async def health(db: DbSession) -> HealthOut:
    """健康检查：探活数据库，并汇报本进程正在跑的 run 数量。

    **刻意不因数据库故障返回 5xx**：DB 探测异常时只是把 ``status`` 降级为
    ``degraded`` 并在 ``db`` 字段带上原因，仍返回 200。这样编排系统能区分
    「进程死了」和「进程活着但依赖异常」，不会把还活着的实例反复重启。

    ``active_runs`` 取自进程内 RunHub，是**单进程视角**；多副本部署时它只是
    本实例的并发量，不是全局总量。
    """
    db_state = "ok"
    try:
        await db.execute(sa.text("SELECT 1"))
    except Exception as exc:  # pragma: no cover
        db_state = f"error: {exc}"

    return HealthOut(
        status="ok" if db_state == "ok" else "degraded",
        app=settings.app_name,
        db=db_state,
        active_runs=len(get_hub()._states),  # noqa: SLF001
        version=__version__,
    )


@router.get("/health", include_in_schema=False)
async def health_root(db: DbSession) -> HealthOut:
    """``/health`` 别名，供负载均衡 / 容器编排探活使用。

    与 ``/api/health`` 完全同源，直接复用 :func:`health` 而非复制逻辑，
    免得两条探活路径的判定标准日后漂移。``include_in_schema=False``：
    它是运维探活入口，不属于对外 API，不该出现在 OpenAPI 里。
    """
    return await health(db)
