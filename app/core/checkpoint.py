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

import logging
from pathlib import Path
from typing import Any

from app.config.settings import settings
from app.core.redis_client import redact_url

logger = logging.getLogger(__name__)

_saver: Any | None = None
_saver_cm: Any | None = None


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
