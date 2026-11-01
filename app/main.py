"""FastAPI 应用工厂（设计文档 §10 main.py）。

生命周期：
- 启动：扫描遗留 running run 标为 error（设计文档 §9）→ 进程崩溃后不会留下
  「永远 running」的死 run；
- 关闭：取消后台 run 任务、冲刷所有未落库事件、关闭连接池。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from redis.exceptions import RedisError

from app import __version__
from app.api.router import api_router
from app.config.db import dispose_engine
from app.config.settings import settings
from app.core.checkpoint import close_checkpointer, init_checkpointer
from app.core.errors import AppError
from app.core.orchestrator import get_orchestrator, recover_stale_runs
from app.core.redis_client import (
    connect_redis,
    disconnect_redis,
    instance_id,
    redis_is_enabled,
    redact_url,
)
from app.core.sensitive import init_filter

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期钩子（FastAPI lifespan）：启动善后 + 关闭收尾。

    **启动**：先把敏感词过滤模型加载好（读不出来直接启动失败）→ 启用 Redis 时
    建立连接（连不上直接启动失败，不静默降级）→ 调 :func:`recover_stale_runs` 把
    上次进程遗留的 running/pending run 统一标成 error。

    **关闭**：先 ``orchestrator.shutdown()``（取消后台 run 任务并等待收尾、
    冲刷内存里尚未落库的事件、关闭 LLM 客户端与 hub），再 ``disconnect_redis()``，
    最后 ``dispose_engine()`` 关连接池。顺序不能反：还在写的任务需要连接可用
    来落库，而 Redis 的 pump 任务需要 Redis 还活着才能干净退出。

    **每一步关闭都独立 try**：以前这几步是平铺的，任何一步抛异常都会让**后面
    的步骤全部跳过**——关停时 ``close_checkpointer`` 抛了，Redis 连接与连接池
    就都泄漏了。关停期的异常不该影响其余资源的释放，所以逐个兜住、逐个记日志。

    **启动失败要回滚**：启动阶段已经建立的资源（Redis 连接、checkpointer）不能
    因为后面某一步失败就悬着——进程还没退出，那些连接会一直占着。

    易踩的坑：启动阶段的**恢复**失败只记日志、不阻塞启动。数据库暂时不可用
    （例如迁移还没跑完）不应该让整个服务起不来，否则就是「恢复逻辑本身制造了
    新的启动故障」。**Redis 不可用、敏感词模型缺失则相反**——必须让启动失败，
    否则会出现「服务能跑但内容没被过滤」或「跨实例收不到事件」这类**症状正常、
    行为已错**的分裂状态，比启动失败难查得多。
    """
    logger.info("%s v%s 启动中…", settings.app_name, __version__)

    try:
        # 敏感词前置过滤（V3.4）放在最前面：它只读一个本地模型文件，是所有启动
        # 检查里最便宜的一个——模型缺失就别急着连 Redis 了。
        try:
            init_filter()
        except Exception as exc:
            logger.error("敏感词过滤模型加载失败：%s", exc)
            raise

        # Redis 连接放在最前面：总线与回放源的单例在第一次使用前必须已就绪，
        # 否则会在某个请求里才炸，错误现场和真正的病因隔得很远。
        if redis_is_enabled():
            try:
                await connect_redis()
            except RedisError as exc:
                logger.error(
                    "Redis 连接失败（%s）：%s。单实例开发请设 REDIS_ENABLED=false。",
                    redact_url(settings.redis_url),
                    exc,
                )
                raise
            logger.info("已启用 Redis 后端，实例标识 %s", instance_id())

        # 审批暂停点（V3.1）。连不上就**直接失败**：没有 checkpointer 就没有「暂停」，
        # 审批节点会卡住不动，用户看到的是「一直在等审批」却永远等不到结果——
        # 那比启动失败难查得多。
        try:
            await init_checkpointer()
        except Exception as exc:
            logger.error("审批暂停点初始化失败：%s", exc)
            raise

        try:
            n = await recover_stale_runs()
            if n:
                logger.warning("已将 %s 个遗留 run 标记为 error", n)
        except Exception:  # pragma: no cover - 数据库暂时不可用时不阻塞启动
            logger.exception("启动时恢复遗留 run 失败（数据库可能尚未就绪）")
    except BaseException:
        # 启动失败：把已经建立的资源回滚掉，别让它们悬到进程退出
        logger.exception("启动失败，回滚已建立的资源")
        await _close_resources()
        raise

    yield
    logger.info("正在关闭…")
    await _close_resources()
    logger.info("已关闭")


async def _close_resources() -> None:
    """逐步关闭各项资源，**每一步独立 try**：一步失败不该让后面的步骤被跳过。

    顺序不能反：还在写的任务需要连接可用来落库，而 Redis 的 pump 任务需要
    Redis 还活着才能干净退出。

    关停期的异常一律吞掉并记日志——关停过程不该把异常抛给 ASGI 服务器，那会
    在已经准备好的响应上再叠一个错误，掩盖真正的关停问题。
    """
    steps = (
        ("编排器", get_orchestrator().shutdown),
        ("审批暂停点", close_checkpointer),
        ("Redis 连接", disconnect_redis),
        ("数据库连接池", dispose_engine),
    )
    for name, closer in steps:
        try:
            await closer()
        except Exception:
            logger.exception("关闭%s失败（继续关闭其余资源）", name)


def create_app() -> FastAPI:
    """应用工厂：装配 CORS、路由、验证台静态页与统一异常处理器。

    抽成工厂而非在模块顶层堆配置，是为了让测试能造多个互不干扰的实例。工厂
    只做装配，不含业务逻辑。

    注意根路由 ``/`` 是**有条件注册**的：``web/index.html`` 不存在（比如生产镜像
    不带 web/ 目录）就不注册，此时 GET / 走 FastAPI 默认的 404，而不是启动报错。
    """
    app = FastAPI(
        title="多 Agent 协作服务",
        version=__version__,
        description=(
            "通过 MySQL 动态配置 Agent 与 Workflow，多 Agent 协作产出结果；"
            "SSE 流式输出，支持心跳保活与断线续传。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Accel-Buffering"],
    )

    app.include_router(api_router)

    # ------------------------------------------------- 静态页（验证台 / 后台）
    # 单文件、无构建步骤；同源提供，方便直接验证 SSE 续传等行为。
    web_dir = Path(__file__).resolve().parents[1] / "web"
    web_index = web_dir / "index.html"
    web_admin = web_dir / "admin.html"
    if web_index.exists():

        @app.get("/", include_in_schema=False)
        async def verification_page() -> FileResponse:
            """功能验证台：直接返回单文件页面 ``web/index.html``。

            刻意做成同源、无构建步骤、零外部依赖：它要用来验证 SSE 续传、心跳、
            断线重连这些行为，任何跨域代理或打包产物都会干扰 EventSource 的真实
            时序。返回 FileResponse 而非 HTMLResponse，是让浏览器自己带
            ETag / 304，改了页面也不用强刷。

            ``include_in_schema=False``：内部验证工具，不属于对外 API 契约。
            """
            return FileResponse(web_index)

    if web_admin.exists():

        @app.get("/admin", include_in_schema=False)
        async def admin_page() -> FileResponse:
            """后台管理页：Agent / Workflow 的增删改，以及会话观测。

            同样零依赖单文件，只调用既有 REST 接口——它不新增任何服务端能力，
            所以和验证台一样按「存在才注册」处理，生产镜像不带 web/ 目录时
            访问 /admin 走默认 404 而不是启动报错。
            """
            return FileResponse(web_admin)

    # ------------------------------------------------------ 统一异常处理

    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        """领域异常 → 设计文档 §8 的错误码约定（400/401/404/409/410）。"""
        body: dict = {"code": exc.code, "message": exc.message}
        if exc.data:
            body["data"] = exc.data
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        """请求参数/请求体校验失败 → 统一 400 响应体。

        FastAPI 默认返回 422，这里改判 400 以对齐设计文档 §8 的错误码约定，让
        「客户端参数有问题」和「请求不合法」不再分裂成两种状态码。

        响应体沿用领域异常那套 ``{code, message, data}`` 结构（``code`` 为
        ``bad_request``），前端只需要一套错误解析逻辑。逐条错误经
        :func:`_safe_errors` 裁剪后再返回，不直接透传 pydantic 原始结构。
        """
        return JSONResponse(
            status_code=400,
            content={
                "code": "bad_request",
                "message": "请求参数校验失败",
                "data": {"errors": _safe_errors(exc)},
            },
        )

    return app


def _safe_errors(exc: RequestValidationError) -> list[dict]:
    """把 pydantic 的原始校验错误裁剪成可安全返回的精简结构。

    坑在这里：``exc.errors()`` 的每项除了 loc/msg/type，还带 ``input``（用户提交的
    原始值）和 ``ctx``。``input`` 里可能是 api_key、system_prompt 或整段对话正文——
    直接整体序列化就等于把敏感内容回显给调用方，还容易连带进日志。所以这里用
    白名单只取三个定位字段，而不是把整个 dict 透传出去。

    ``loc`` 里的 ``body`` / ``query`` 等键不是字符串，统一 ``str()`` 掉，避免
    JSON 序列化时出现意外类型。

    Args:
        exc: FastAPI 的 ``RequestValidationError``。

    Returns:
        每项含 loc / msg / type 的 dict 列表。
    """
    out = []
    for err in exc.errors():
        out.append(
            {
                "loc": [str(x) for x in err.get("loc", ())],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
        )
    return out


app = create_app()
