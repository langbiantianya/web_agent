"""健康检查。"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from fastapi import APIRouter

from app import __version__
from app.api.deps import DbSession
from app.config.settings import settings
from app.core.redis_client import get_redis, redis_is_enabled
from app.core.run_hub import get_hub
from app.schemas.common import HealthOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthOut)
async def health(db: DbSession) -> HealthOut:
    """健康检查：探活数据库与 Redis，汇报本进程正在跑的 run 数量。

    **刻意不因依赖故障返回 5xx**：探测异常时只是把 ``status`` 降级为
    ``degraded`` 并在对应字段带上原因，仍返回 200。这样编排系统能区分
    「进程死了」和「进程活着但依赖异常」，不会把还活着的实例反复重启。

    **Redis 也要探**：多实例下 Redis 是事件总线与租约的底座，它挂了不代表
    进程死了，但这个实例已经**不能正常服务**了——只探 DB 的话编排系统会继续
    把流量打过来，用户看到的是「一直转圈但没有结果」。

    **不把原始异常回给调用方**：驱动异常里常带着数据库主机、端口、用户名与
    内部文档链接，这些不该出现在一个未鉴权的探活响应里。细节进日志，
    对外只给「哪个依赖不健康」。

    ``active_runs`` 取自进程内 RunHub，是**单进程视角**；多副本部署时它只是
    本实例的并发量，不是全局总量。
    """
    db_state = "ok"
    try:
        await db.execute(sa.text("SELECT 1"))
    except Exception as exc:
        logger.warning("健康检查：数据库探测失败：%s", exc, exc_info=True)
        db_state = "error"

    redis_state = "disabled"
    if redis_is_enabled():
        redis_state = "ok"
        try:
            await (await get_redis()).ping()
        except Exception as exc:
            logger.warning("健康检查：Redis 探测失败：%s", exc, exc_info=True)
            redis_state = "error"

    states = [db_state]
    if redis_is_enabled():
        states.append(redis_state)
    status = "ok" if all(s in ("ok", "disabled") for s in states) else "degraded"

    return HealthOut(
        status=status,
        app=settings.app_name,
        db=db_state,
        redis=redis_state,
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
