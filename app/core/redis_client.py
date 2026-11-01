"""Redis 连接管理、实例身份与键布局（V2 多实例基础设施）。

本模块只做三件事，**不含任何业务逻辑**：

1. **连接生命周期**：``connect_redis`` / ``disconnect_redis`` / ``get_redis``，
   由 :mod:`app.main` 的 lifespan 调用。连接不可用时**直接抛错终止启动**，
   不做静默降级——见 :attr:`Settings.redis_enabled` 的说明。
2. **实例身份**：``instance_id()`` 是本进程的唯一标识（``主机名:PID:随机后缀``），
   用于 run 的归属判定。同一台机器上起两个实例时 PID 不同，跨机器则主机名不同。
3. **键布局**：所有 Redis 键与频道名的**唯一定义处**。键名前缀来自
   ``REDIS_KEY_PREFIX``，多套环境可以共享同一个 Redis 而互不干扰。

键布局（``P`` 为 ``REDIS_KEY_PREFIX``，默认 ``webagent``）：

==========================  ==========================================
``P:run:<id>``              run 的事件 Pub/Sub 频道（实时推送）
``P:run:<id>:ev``           run 的事件 Stream（**回放源**，长期归档在 MySQL）
``P:run:<id>:ctl``          run 的控制频道（取消信号等指令）
``P:run:<id>:active``       活动标记，生成方周期续期，带 TTL
``P:run:<id>:owner``        归属实例标记，带 TTL，启动恢复据此回收无主 run
==========================  ==========================================

Stream 的 ID 固定写成 ``<seq>-0``：Redis 要求新 ID 严格大于当前末位，而 seq 在
单个 run 内单调递增，因此可以直接用 ``XRANGE key (seq + → +`` 做「断点续传」，
不必再按字段过滤。seq 由生成方唯一分配（见 :mod:`app.core.run_hub` 的单写者约定），
所以这个前提成立。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError

from app.config.settings import settings

logger = logging.getLogger(__name__)

# 本进程标识。同一次运行内不变，跨进程/跨机器唯一。
_INSTANCE_ID: str | None = None

# 全局连接池与客户端。由 lifespan 负责创建与关闭。
_pool: ConnectionPool | None = None
_client: Redis | None = None


def redis_is_enabled() -> bool:
    """本进程是否启用 Redis 后端。

    取配置而非「客户端是否已创建」：在 lifespan 建立连接之前就该能回答，
    否则启动早期的代码分支判断会和实际状态不一致。
    """
    return settings.redis_enabled


def instance_id() -> str:
    """返回本进程的实例标识，形如 ``myhost-1234-a1b2c3``。

    三段分别对应主机名、PID、随机后缀：

    - 主机名区分**跨机器**部署；
    - PID 区分**同机多实例**（同机不同进程）；
    - 随机后缀兜住 PID 复用的情况——机器重启后老 PID 可能被分给别的进程，
      没有后缀就会把别人的 run 误认成自己的。

    进程生命周期内缓存，避免每次调用都做一次 ``getpid`` + ``uuid4``。
    """
    global _INSTANCE_ID
    if _INSTANCE_ID is None:
        _INSTANCE_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        logger.info("本实例标识：%s", _INSTANCE_ID)
    return _INSTANCE_ID


# --------------------------------------------------------------------- 键布局


def _ns(*parts: str) -> str:
    """拼接带前缀的 Redis 键/频道名。"""
    return ":".join((settings.redis_key_prefix, *parts))


def run_channel(run_id: int) -> str:
    """run 的事件 Pub/Sub 频道。

    与 :func:`app.core.run_hub.channel_of` 的 ``run:<id>`` 保持同样的形状，
    只是多了环境前缀。两者的对应关系由 ``tests/test_redis.py`` 钉住，
    改一边必须改另一边。
    """
    return _ns("run", str(run_id))


def run_stream(run_id: int) -> str:
    """run 的事件 Stream——回放的权威来源。"""
    return _ns("run", str(run_id), "ev")


def run_ctl_channel(run_id: int) -> str:
    """run 的控制频道（取消等指令）。"""
    return _ns("run", str(run_id), "ctl")


def run_active_key(run_id: int) -> str:
    """run 的活动标记键（带 TTL，由生成方续期）。"""
    return _ns("run", str(run_id), "active")


def run_owner_key(run_id: int) -> str:
    """run 的归属实例标记（带 TTL），启动恢复据此判断是否「无主」。"""
    return _ns("run", str(run_id), "owner")


# ------------------------------------------------------------------- 生命周期


async def get_redis() -> Redis:
    """取全局 Redis 客户端，未初始化时抛错。

    刻意不惰性创建：连接应当在 lifespan 里显式建立，这样启动阶段就能把
    「Redis 配错了」暴露成一个明确的启动失败，而不是等到第一个请求才炸。
    """
    if _client is None:
        raise RuntimeError("Redis 客户端未初始化（lifespan 未调用 connect_redis）")
    return _client


async def connect_redis(attempts: int = 3, backoff: float = 0.5) -> Redis:
    """建立连接池并做一次 PING 探活，失败时按 ``backoff`` 退避重试。

    ``health_check_interval`` 让空闲连接定期自检，避免长时间没有请求后
    拿到一条已被对端关掉的死连接——这类问题在长连接服务里最难查。

    带重试是因为 Redis 偶尔会瞬时不可达（服务端重启、主从切换、网络抖动）。
    实测遇到过一轮测试跑到一半 Redis 拒连，直接失败就是 19 个用例全部 ERROR，
    而两秒后它又好了。重试能吸收这种短抖动，同时**仍然不做降级**：重试全部失败
    就照常抛错，让调用方（lifespan）终止启动。

    Raises:
        RedisError: 重试 ``attempts`` 次后仍连不上或认证失败。
    """
    global _pool, _client
    if _client is not None:
        return _client

    last_error: RedisError | None = None
    for attempt in range(1, max(1, attempts) + 1):
        pool = ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=5,
            # 读也必须有上限：``socket_connect_timeout`` 只管「连不连得上」，
            # 连上之后的读**默认无限等**。Redis 所在网段一抖，XADD / PUBLISH
            # 里的一个读就能把整条生成流程冻住——外部看是「服务没反应了」，
            # 日志里却什么都看不出来。宁可 5 秒后报个错重试，也不要无限期挂死。
            socket_timeout=settings.redis_socket_timeout,
            socket_keepalive=True,
        )
        client = Redis(connection_pool=pool)
        try:
            await client.ping()
        except RedisError as exc:
            last_error = exc
            # 连接池已建但探活失败，必须拆掉重来，否则下次重试会拿到半初始化状态。
            # 注意 ``disconnect()`` 在 redis.asyncio 里是协程，必须 await——
            # 忘了 await 只会留下 "coroutine was never awaited" 警告，连接却没关。
            try:
                await client.aclose()
                await pool.disconnect()
            except Exception:  # pragma: no cover - 清理失败不影响重试判定
                logger.debug("建连失败后的清理出错", exc_info=True)
            if attempt < attempts:
                delay = backoff * attempt
                logger.warning(
                    "Redis 连接失败（第 %s/%s 次）：%s，%.1fs 后重试",
                    attempt, attempts, exc, delay,
                )
                await asyncio.sleep(delay)
            continue

        _pool, _client = pool, client
        logger.info("Redis 已连接：%s", redact_url(settings.redis_url))
        return _client

    assert last_error is not None
    _pool, _client = None, None
    raise last_error


async def disconnect_redis() -> None:
    """关闭客户端与连接池。幂等，可重复调用。

    先关客户端再拆池子：客户端的 ``aclose()`` 会归还它持有的连接，之后再
    ``disconnect()`` 池子才不会漏掉仍在使用的连接。两者都是协程，漏掉 await
    是这条路径上最容易犯的错（表现为警告刷屏但连接其实没释放）。
    """
    global _pool, _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # pragma: no cover - 关停期异常不应影响退出
            logger.exception("关闭 Redis 客户端失败")
        _client = None
    if _pool is not None:
        try:
            await _pool.disconnect()
        except Exception:  # pragma: no cover
            logger.exception("关闭 Redis 连接池失败")
        _pool = None
    logger.info("Redis 连接已关闭")


def redact_url(url: str) -> str:
    """隐藏 URL 里的密码，只留 host:port 便于排查。

    ``redis://:secret@host:6379/0`` → ``redis://***@host:6379/0``。

    公开给别的模块用：启动日志、checkpointer 的初始化日志都碰得到这个 URL，
    而密码进日志等于把凭证抄送给了日志采集系统。
    """
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***@{hostpart}"
