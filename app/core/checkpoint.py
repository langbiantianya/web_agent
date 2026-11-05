"""LangGraph checkpointer：审批暂停点的存放处（V3.1，设计文档 §16）。

## 它存什么、不存什么

**存**：图暂停在哪个节点、那一刻的状态（context / upstream / usage）。

**不存**：对外事件流。事件仍然只由 ``run_event`` + Redis Stream 负责（见
:mod:`app.core.event_log`）。这是刻意的分工——checkpointer 回答「暂停在哪」，
事件日志回答「发生过什么」。两者不重叠，也就不存在「两个真相来源互相矛盾」
的问题。

## 为什么有两套实现

| 场景 | 实现 | 理由 |
| --- | --- | --- |
| 单实例开发（``REDIS_ENABLED=false``） | ``AsyncSqliteSaver`` | 零外部依赖，一个本地文件 |
| 多实例（``REDIS_ENABLED=true``） | ``AsyncRedisSaver`` | 暂停点必须**所有实例都能看到**：A 实例上挂起、B 实例上收到审批要能续上 |

选 Redis 而不是 MySQL：LangGraph 没有官方 MySQL saver，而本项目已经在用 Redis
做多实例协调；再引一个 saver 实现等于多一套要维护的代码。

**与 Redis 不可达的取舍**：checkpointer 不可用时，审批功能直接报错而不是静默
降级——没有 checkpointer 就没有「暂停」，run 会卡在审批节点上，用户只能看到
「在等审批」却永远等不到结果。那种状态比明确报错难查得多。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config.settings import settings
from app.core.redis_client import redact_url

logger = logging.getLogger(__name__)

_saver: Any | None = None
_saver_cm: Any | None = None
_cleanup_task: asyncio.Task | None = None


def checkpoint_enabled() -> bool:
    """是否启用审批暂停点。

    关掉它时 :func:`build_run_graph` 仍能跑普通的 DAG/条件边流程——只是**审批
    节点不可用**：走到审批节点会直接报错，而不是无声地跳过。
    """
    return settings.checkpoint_enabled


async def init_checkpointer() -> Any | None:
    """建立并初始化 checkpointer（应用启动时调用）。

    Returns:
        初始化好的 saver；``checkpoint_enabled=false`` 时返回 None。

    Raises:
        Exception: 底层存储连不上时原样抛出。**不静默降级**——理由见模块开头。
    """
    global _saver, _saver_cm

    if _saver is not None:
        return _saver
    if not settings.checkpoint_enabled:
        logger.info("审批暂停点已关闭（CHECKPOINT_ENABLED=false）")
        return None

    if settings.redis_enabled:
        from langgraph.checkpoint.redis import AsyncRedisSaver

        # **必须显式传 connection_args**：``from_conn_string`` 不传时用的是
        # Redis 客户端的默认超时——读**没有上限**。Redis 所在网段一抖，saver 的
        # 一次读就能把整条 run 冻住，而 saver 就在 run 的执行路径上
        # （图每次超步都要读写暂停点），于是 run 永久卡死、日志里什么都看不到。
        # 这正是 settings.redis_socket_timeout 的注释里特意提醒过的场景。
        _saver_cm = AsyncRedisSaver.from_conn_string(
            settings.redis_url,
            checkpoint_prefix=f"{settings.redis_key_prefix}:checkpoint",
            checkpoint_write_prefix=f"{settings.redis_key_prefix}:checkpoint-write",
            connection_args={
                "socket_timeout": settings.redis_socket_timeout,
                "socket_connect_timeout": settings.redis_socket_timeout,
                "health_check_interval": 30,
            },
        )
        _saver = await _saver_cm.__aenter__()
        try:
            await _saver.asetup()
        except Exception as exc:
            # asetup 会建 RediSearch 索引，普通 redis-server 没有这个模块，
            # 报错是 "unknown command 'FT.CREATE'" —— 不加这层包装，用户看到
            # 的是一句与「你装的是普通 Redis，不是 Redis Stack」毫无关系的话。
            await close_checkpointer()
            raise RuntimeError(
                "审批暂停点初始化失败：Redis 侧需要 RediSearch 模块（Redis Stack）。"
                "若用的是普通 redis-server，请改用 CHECKPOINT_ENABLED=false"
                "（放弃审批功能）或换 Redis Stack。原始错误：" + str(exc)
            ) from exc
        logger.info(
            "审批暂停点使用 Redis saver：%s", redact_url(settings.redis_url)
        )
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        # **传纯文件路径，不传 SQLAlchemy 风格的 URL**：AsyncSqliteSaver 会把
        # 字符串原样交给 aiosqlite.connect()，后者只认路径或 file: URI，
        # 认不得 "sqlite+aiosqlite:///..."，会直接报 "unable to open database file"。
        path = _resolve_sqlite_path(settings.checkpoint_sqlite_path)
        _saver_cm = AsyncSqliteSaver.from_conn_string(path)
        _saver = await _saver_cm.__aenter__()
        await _saver.setup()
        logger.info("审批暂停点使用 SQLite saver：%s", path)

    return _saver


async def close_checkpointer() -> None:
    """关闭 checkpointer（应用退出时调用）。"""
    global _saver, _saver_cm

    await stop_checkpoint_cleanup()
    if _saver_cm is not None:
        try:
            await _saver_cm.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - 收尾失败不该影响退出
            logger.exception("关闭 checkpointer 失败")
    _saver = None
    _saver_cm = None


def get_checkpointer() -> Any | None:
    """取全局 checkpointer。

    ``None`` 表示审批暂停点未启用——调用方（编排器）据此拒绝审批节点，而不是
    默默把审批当成普通节点跑过去。
    """
    return _saver


def thread_id_for(run_id: int) -> str:
    """run → checkpointer 的 thread_id。

    用 run_id 而不是会话 id：一个会话可以有多次 run，暂停点属于**这一次 run**。
    """
    return f"run-{run_id}"


def _resolve_sqlite_path(path: str) -> str:
    """相对路径按**项目根**解析，绝对路径原样返回。

    为什么不用工作目录：``checkpoints.db`` 装的是「图暂停在哪」。用不同的
    WORKDIR 启动两个实例（systemd、Docker、``uvicorn --app-dir`` 指向不同目录）
    会各写各的文件，于是 A 实例上挂起的审批，B 实例根本看不到——用户提交了
    决策却毫无反应。同一个口径在 :attr:`Settings.sensitive_model_path` 上
    已经用过了。
    """
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(Path(__file__).resolve().parents[2] / p)


def checkpoint_config(run_id: int, recursion_limit: int) -> dict:
    """构造 ``ainvoke`` 用的 config。

    ``thread_id`` 是 checkpointer 的定位键：暂停后恢复必须用同一个 thread_id，
    否则 LangGraph 找不到那次暂停，会当成一次全新的执行从头跑。
    """
    return {
        "configurable": {"thread_id": thread_id_for(run_id)},
        "recursion_limit": recursion_limit,
    }


async def delete_thread(run_id: int) -> None:
    """删掉某次 run 的全部 checkpoint。

    run 到达终态时调用：暂停点已经没有用了，留着只会让 Redis / SQLite 里的
    数据无限堆积。删不掉不算致命——只是留下垃圾数据，不影响正确性。
    """
    saver = get_checkpointer()
    if saver is None:
        return
    try:
        await saver.adelete_thread(thread_id_for(run_id))
    except Exception:  # pragma: no cover - 清理失败不影响结果
        logger.exception("删除 run %s 的 checkpoint 失败", run_id)


def _run_id_from_thread(thread_id: str) -> int | None:
    """从 ``run-<id>`` thread 名解析 run id；其它 thread 不属于本服务。"""
    if not thread_id.startswith("run-"):
        return None
    try:
        return int(thread_id.removeprefix("run-"))
    except ValueError:
        return None


def _checkpoint_time(tuple_) -> datetime | None:
    """读取 checkpoint 的 UTC 时间；格式不认识时返回 None。"""
    raw = (getattr(tuple_, "checkpoint", None) or {}).get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def cleanup_orphan_checkpoints() -> int:
    """清理终态 run 或超期无主 run 的 checkpoint thread。

    正常路径会在 run 收尾时立即删除 thread。本巡检兜住三类失败：进程在删除前
    被硬杀、删除调用临时失败、数据库记录已经不存在但 thread 仍留在存储里。

    缺失 run 的 thread 必须超过 ``checkpoint_orphan_retention_sec`` 才删除，
    避免 run 和 checkpoint 跨两个存储、写入有先后时误删刚创建的数据。

    Returns:
        成功删除的 thread 数。扫描或单项删除失败只记日志，不阻断服务。
    """
    saver = get_checkpointer()
    if saver is None:
        return 0

    threads: dict[str, datetime | None] = {}
    try:
        async for item in saver.alist(
            None, limit=max(1, settings.checkpoint_cleanup_scan_limit)
        ):
            config = getattr(item, "config", None) or {}
            thread_id = (config.get("configurable") or {}).get("thread_id")
            if not isinstance(thread_id, str):
                continue
            stamp = _checkpoint_time(item)
            previous = threads.get(thread_id)
            if previous is None or (stamp is not None and stamp > previous):
                threads[thread_id] = stamp
    except Exception:
        logger.exception("扫描 checkpoint 失败")
        return 0

    run_ids = [
        run_id
        for thread_id in threads
        if (run_id := _run_id_from_thread(thread_id)) is not None
    ]
    if not run_ids:
        return 0

    try:
        import sqlalchemy as sa

        from app.config.db import SessionLocal
        from app.models.run import Run

        async with SessionLocal() as db:
            rows = await db.execute(
                sa.select(Run.id, Run.status).where(Run.id.in_(run_ids))
            )
            statuses = {int(run_id): status for run_id, status in rows}
    except Exception:
        logger.exception("读取 checkpoint 对应 run 状态失败")
        return 0

    from app.models.enums import RunStatus

    now = datetime.now(UTC)
    terminal = {status.value for status in RunStatus if status.terminal}
    retention = max(0, settings.checkpoint_orphan_retention_sec)
    deleted = 0
    for thread_id, stamp in threads.items():
        run_id = _run_id_from_thread(thread_id)
        if run_id is None:
            continue
        status = statuses.get(run_id)
        should_delete = status in terminal
        if status is None and stamp is not None:
            should_delete = (now - stamp).total_seconds() >= retention
        if not should_delete:
            continue
        try:
            await saver.adelete_thread(thread_id)
            deleted += 1
        except Exception:
            logger.exception("删除孤儿 checkpoint thread %s 失败", thread_id)
    if deleted:
        logger.info("已清理 %s 个孤儿 checkpoint thread", deleted)
    return deleted


def start_checkpoint_cleanup() -> None:
    """启动周期清理任务；未启用 checkpointer 时为空操作。"""
    global _cleanup_task

    if not settings.checkpoint_enabled or not settings.checkpoint_cleanup_enabled:
        return
    if _cleanup_task is not None and not _cleanup_task.done():
        return
    interval = max(1.0, settings.checkpoint_cleanup_interval_sec)
    _cleanup_task = asyncio.create_task(_checkpoint_cleanup_loop(interval))
    logger.info("checkpoint 孤儿清理已启动，间隔 %.1f 秒", interval)


async def _checkpoint_cleanup_loop(interval: float) -> None:
    """周期清理；单轮失败不影响下一轮。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await cleanup_orphan_checkpoints()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("checkpoint 孤儿清理巡检失败（下一周期继续）")


async def stop_checkpoint_cleanup() -> None:
    """停止周期清理任务。"""
    global _cleanup_task

    task = _cleanup_task
    _cleanup_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
