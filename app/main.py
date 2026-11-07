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
from app.core.body_limit import BodySizeLimitMiddleware, scope_header
from app.core.approval_timeout import (
    expire_due_approvals,
    start_approval_timeout_monitor,
    stop_approval_timeout_monitor,
)
from app.core.checkpoint import (
    cleanup_orphan_checkpoints,
    close_checkpointer,
    init_checkpointer,
    start_checkpoint_cleanup,
)
from app.core.errors import AppError
from app.core.mcp.manager import mcp_manager
from app.core.orchestrator import get_orchestrator, recover_stale_runs
from app.core.redis_client import (
    connect_redis,
    disconnect_redis,
    instance_id,
    redis_search_available,
    redis_is_enabled,
    redact_url,
)
from app.core.sensitive import init_filter
from app.core.studio import (
    StudioStartupError,
    close_studio_client,
    init_studio,
)
from app.core.tool_audit import (
    cleanup_expired_tool_audits,
    start_tool_audit_cleanup,
    stop_tool_audit_cleanup,
)
from app.core.trace import (
    current_traceparent,
    reset_traceparent,
    set_traceparent,
)
from app.core.tools import set_optional_tools_enabled

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _admin_elevated(scope) -> bool:
    """请求是否拥有可上传 Skill 的后台 Session 与权限。"""
    from app.config.db import SessionLocal
    from app.core.admin_auth import resolve_admin_request
    from app.core.admin_authz import casbin_service
    from starlette.requests import Request

    if not scope_header(scope, "cookie"):
        return False
    try:
        async with SessionLocal() as db:
            principal = await resolve_admin_request(db, Request(scope))
            if principal is None:
                return False
            if principal.is_platform_super_admin:
                return True
            return await casbin_service.can(
                db,
                user_id=principal.user_id or "",
                tenant_id=principal.tenant_id or settings.auth_default_tenant,
                object_name="skill",
                action="write",
            )
    except Exception:  # noqa: BLE001 - 请求体限制必须 fail-closed
        return False


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
    新的启动故障」。**Redis 不可用、敏感词模型缺失、studio 不可达则相反**——
    必须让启动失败，否则会出现「服务能跑但内容没被过滤」「跨实例收不到事件」
    或「脚本执行能力看着有其实没有」这类**症状正常、行为已错**的分裂状态，
    比启动失败难查得多。
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
            if settings.checkpoint_enabled and not await redis_search_available():
                raise RuntimeError(
                    "REDIS_ENABLED=true 且 CHECKPOINT_ENABLED=true，但当前 Redis "
                    "不提供 RediSearch FT.* 命令。事件总线可使用普通 Redis；"
                    "审批暂停点请改用 Redis Stack，或显式设 "
                    "CHECKPOINT_ENABLED=false。"
                )

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

        # 启动恢复把崩溃前的 running run 标成 error 之后，再清掉 checkpointer
        # 里对应的暂停点。周期任务继续兜住删除失败与其它无主 thread。
        try:
            await cleanup_orphan_checkpoints()
        except Exception:  # pragma: no cover - 清理失败不阻塞启动
            logger.exception("启动时清理 checkpoint 孤儿失败")
        start_checkpoint_cleanup()
        try:
            await expire_due_approvals()
        except Exception:  # pragma: no cover - 超时巡检不阻塞启动
            logger.exception("启动时审批超时巡检失败")
        start_approval_timeout_monitor()
        mcp_manager.start_idle_reaper()
        try:
            await cleanup_expired_tool_audits()
        except Exception:  # pragma: no cover - 清理失败不阻塞启动
            logger.exception("启动时清理过期工具审计失败")
        start_tool_audit_cleanup()

        # studio 脚本执行（V4，§22.8.6）。配了地址但连不上 → **启动失败**：
        # studio 是部署的一个组成部分，它挂了说明部署不完整，让问题在部署时暴露，
        # 而不是等到用户的一次 run 里才炸。
        # 连不上且 STUDIO_EXEC_REQUIRED=false 时不注册 skill_exec——模型看不见它、
        # 配置期也拒，不会留下「配了但调不通」的哑配置。
        try:
            studio_enabled = await init_studio()
        except StudioStartupError as exc:
            logger.error("studio 不可用：%s", exc)
            raise
        if studio_enabled:
            await _audit_skill_runtime()
        set_optional_tools_enabled(studio_enabled)
    except BaseException:
        # 启动失败：把已经建立的资源回滚掉，别让它们悬到进程退出
        logger.exception("启动失败，回滚已建立的资源")
        await _close_resources()
        raise

    yield
    logger.info("正在关闭…")
    await _close_resources()
    logger.info("已关闭")


async def _audit_skill_runtime() -> None:
    """启动期对账：被 Agent 绑定的技能，在本部署能不能执行。

    为什么不在 ``init_studio`` 里做：它不碰数据库，只负责 studio 侧（版本、
    ``ready``、能力）。技能侧要查库，放在这里、等数据库可用之后。

    **数据库暂时不可用不阻塞启动**（与 ``recover_stale_runs`` 同口径）；
    但如果数据库是好的、只是技能配置错了，那就必须启动失败——配置错了应该在
    部署时暴露，而不是等模型第一次调用。

    Raises:
        StudioStartupError: 有技能声明 isolated，但本部署跑不了，且
            ``studio_exec_required`` 为真。
    """
    from app.config.db import SessionLocal
    from app.core.skill_runtime import audit_skill_runtime_bindings
    from app.core.studio import get_studio_client

    client = get_studio_client()
    if client is None or client.health_info is None:
        return
    try:
        async with SessionLocal() as db:
            await audit_skill_runtime_bindings(
                db,
                client.health_info,
                required=settings.studio_exec_required,
            )
    except StudioStartupError:
        raise
    except Exception:  # pragma: no cover - 数据库暂时不可用时不阻塞启动
        logger.exception("启动期技能运行档审计失败（数据库可能尚未就绪）")


async def _close_resources() -> None:
    """逐步关闭各项资源，**每一步独立 try**：一步失败不该让后面的步骤被跳过。

    顺序不能反：还在写的任务需要连接可用来落库，而 Redis 的 pump 任务需要
    Redis 还活着才能干净退出。

    关停期的异常一律吞掉并记日志——关停过程不该把异常抛给 ASGI 服务器，那会
    在已经准备好的响应上再叠一个错误，掩盖真正的关停问题。
    """
    steps = (
        ("编排器", get_orchestrator().shutdown),
        # studio 通道排在编排器**之后**：编排器要取消还在跑的 run 任务，
        # 那些任务可能正在调 studio——通道先关了，调用会变成「连不上」。
        ("studio 连接", close_studio_client),
        ("MCP 连接", mcp_manager.shutdown),
        ("审批暂停点", close_checkpointer),
        ("审批超时巡检", stop_approval_timeout_monitor),
        ("工具审计清理", stop_tool_audit_cleanup),
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

    # 请求体上限先挂（它在外层），CORS 后挂（它更外层）——这样 413 响应也带得上
    # CORS 头，前端才读得到错误体而不是一个「跨域失败」。
    #
    # 技能上传路径有专属上限（V4，§22.10），但**只对有效后台 Session 且具备
    # skill.write 的请求生效**：
    # 技能包导入与资源上传需要比全局 2 MB 更大的请求体，而无条件放宽等于给
    # 未认证的调用方开了一个 8 MB 的缓冲口。
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        path_limits={"/api/skills": settings.skill_upload_max_bytes},
        elevated_allowed=_admin_elevated,
    )
    app.add_middleware(
        CORSMiddleware,
        # 登录态走 Cookie，浏览器 CORS 不能再使用 ``*`` + credentials。默认
        # 只支持同源；跨域部署必须显式配置 AUTH_ALLOWED_ORIGINS。
        allow_origins=[origin.rstrip("/") for origin in settings.auth_allowed_origins],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Accel-Buffering"],
    )

    app.include_router(api_router)

    @app.middleware("http")
    async def trace_context_middleware(request: Request, call_next):
        """接收并回写 W3C traceparent，供工具执行链路继续传播。"""
        incoming = request.headers.get("traceparent")
        token = set_traceparent(incoming)
        try:
            response = await call_next(request)
            response.headers["traceparent"] = current_traceparent() or ""
            return response
        finally:
            reset_traceparent(token)

    # ------------------------------------------------- 静态页（验证台 / 后台）
    # 单文件、无构建步骤；同源提供，方便直接验证 SSE 续传等行为。
    web_dir = Path(__file__).resolve().parents[1] / "web"
    web_index = web_dir / "index.html"
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
