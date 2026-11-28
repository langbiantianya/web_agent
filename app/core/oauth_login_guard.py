"""OAuth 登录链路的限频、失败记账、事务清理与 provider 故障监控（V4.10 加固）。

## 为什么需要这一层

登录链路原先是**裸**的：`/api/auth/oauth/{code}/start` 每次都新建一笔登录事务，
`/callback` 每次都去 IdP 换一次 token。两者都没有任何频次限制，于是：

- 攻击者可以无限次拉起 authorization-code 流程去撞库、打 IdP（凭据爆破、
  IdP 侧配额消耗、我们自己的 token endpoint 被打满）；
- `oauth_login_attempt` 表**只增不删**——它同时是限频的计数来源，不清理就
  既是数据泄漏也是计数漂移。

这一层提供三件事：

1. **限频**：按「IP × 供应商」计数，两道闸——start 闸限制登录事务的拉起频率，
   callback 闸限制失败次数。
2. **失败记账**：每次失败把**稳定错误码**写回那一行事务（不写原文、不写 token）。
3. **provider 故障监控**：按窗口统计每个供应商的失败率，超阈值告警。

## 几条硬约束

- **不存明文 IP**：只存掺了 ``SECRET_KEY`` 的 SHA-256 哈希。
- **默认不信任 ``X-Forwarded-For``**：这个头由客户端自由填写，认它等于让攻击者
  自己挑限频的分组键。只有确定前面有可信反代时才通过配置打开。
- **限频不区分成功失败之外的东西**：计数窗口用「行数」，所以清理任务必须先跑，
  否则计数会一直累加直到所有人都被限。清理周期与限频窗口的关系见
  ``oauth_attempt_retention_days``。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa
from fastapi import Request

from app.config.settings import settings
from app.core.errors import TooManyRequestsError
from app.models.base import utcnow
from app.models.oauth import OAuthLoginAttempt, OAuthProvider

logger = logging.getLogger(__name__)

_cleanup_task: asyncio.Task | None = None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """某个供应商在窗口内的登录健康状况。"""

    provider_code: str
    attempts: int
    failures: int
    failure_ratio: float
    top_failure: str

    @property
    def alerting(self) -> bool:
        """是否达到告警门槛。"""
        return (
            self.failures >= settings.oauth_provider_failure_alert_min_count
            and self.failure_ratio >= settings.oauth_provider_failure_alert_ratio
        )


def client_ip(request: Request) -> str:
    """取用于限频分组的客户端 IP。

    默认只用连接层的 ``request.client.host``——**不认** ``X-Forwarded-For``：
    这个头是客户端可以随便填的，认它等于让攻击者自己决定限频的分组键，
    限频形同虚设。确实部署在可信反代后面时，用
    ``OAUTH_TRUSTED_PROXY_HEADERS=true`` 打开，届时取 XFF 的**第一段**
    （最靠近客户端的那一跳）。
    """
    if settings.oauth_trusted_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    client = request.client
    return client.host if client else "unknown"


def ip_hash(ip: str) -> str:
    """把客户端 IP 哈希成可入库的分组键。

    掺入 ``SECRET_KEY``：没有它，攻击者拿到库之后可以直接对常见网段做字典
    爆破把 IP 反推出来。
    """
    raw = f"{settings.secret_key}:{ip}".encode()
    return hashlib.sha256(raw).hexdigest()


async def _count_since(
    db,
    provider_id: int,
    ip: str,
    since,
    *,
    only_failures: bool,
) -> int:
    """统计窗口内某个 IP 在某个供应商下的行数。"""
    stmt = sa.select(sa.func.count()).select_from(OAuthLoginAttempt).where(
        OAuthLoginAttempt.provider_id == provider_id,
        OAuthLoginAttempt.client_ip_hash == ip,
        OAuthLoginAttempt.created_at >= since,
    )
    if only_failures:
        stmt = stmt.where(
            OAuthLoginAttempt.failure_code.is_not(None),
            OAuthLoginAttempt.failed_at >= since,
        )
    return int(await db.scalar(stmt) or 0)


async def check_start_rate_limit(db, provider_id: int, ip: str) -> None:
    """start 闸：限制单个 IP 在窗口内拉起多少次登录事务。

    超过阈值抛 :class:`TooManyRequestsError`（429）。
    """
    if not settings.oauth_login_rate_limit_enabled:
        return
    window = max(1, settings.oauth_start_rate_limit_window_sec)
    since = utcnow() - timedelta(seconds=window)
    count = await _count_since(
        db, provider_id, ip, since, only_failures=False
    )
    limit = max(1, settings.oauth_start_rate_limit_max)
    if count >= limit:
        logger.warning(
            "OAuth 登录拉起被限频：provider_id=%s ip_hash=%s 窗口内 %s 次",
            provider_id,
            ip[:12],
            count,
        )
        raise TooManyRequestsError(
            "登录请求过于频繁，请稍后再试",
            data={"retry_after": window},
        )


async def check_callback_failure_limit(db, provider_id: int, ip: str) -> None:
    """callback 闸：限制单个 IP 在窗口内的登录失败次数。

    超过阈值同样抛 429（由路由层转成 ``rate_limited`` 错误码跳转）。
    """
    if not settings.oauth_login_rate_limit_enabled:
        return
    window = max(1, settings.oauth_callback_failure_limit_window_sec)
    since = utcnow() - timedelta(seconds=window)
    count = await _count_since(
        db, provider_id, ip, since, only_failures=True
    )
    limit = max(1, settings.oauth_callback_failure_limit_max)
    if count >= limit:
        logger.warning(
            "OAuth 登录失败次数超限：provider_id=%s ip_hash=%s 窗口内 %s 次",
            provider_id,
            ip[:12],
            count,
        )
        raise TooManyRequestsError(
            "登录失败次数过多，请稍后再试",
            data={"retry_after": window},
        )


async def record_failure(
    db,
    attempt: OAuthLoginAttempt,
    code: str,
    *,
    ip: str | None = None,
) -> None:
    """把一次登录失败记到对应的事务行上。

    只写**稳定错误码**（``provider_error`` / ``login_failed`` / …），不写
    provider 原文、token 或异常堆栈。``ip`` 给了就一并补上——回调请求的 IP
    与拉起时通常一致，但不保证；补写能让失败限频统计到正确的分组。
    """
    attempt.failure_code = code[:32]
    attempt.failed_at = utcnow()
    if ip and not attempt.client_ip_hash:
        attempt.client_ip_hash = ip
    await db.commit()


async def provider_health(session_factory=None) -> list[ProviderHealth]:
    """按窗口统计每个供应商的登录失败率。

    返回值按失败数从多到少排。统计口径：**窗口内新建的事务**里有多少条带
    失败码。

    ``session_factory`` 用于指定查哪个库：客户端应用用主库，管理端用管理库
    （两边的 ``oauth_login_attempt`` 各是各的）。
    """
    window = max(1, settings.oauth_provider_failure_alert_window_sec)
    since = utcnow() - timedelta(seconds=window)

    rows = (
        await db_execute_all(
            sa.select(
                OAuthLoginAttempt.provider_id,
                sa.func.count().label("total"),
                sa.func.sum(
                    sa.case((OAuthLoginAttempt.failure_code.is_not(None), 1), else_=0)
                ).label("failed"),
            )
            .where(OAuthLoginAttempt.created_at >= since)
            .group_by(OAuthLoginAttempt.provider_id),
            session_factory,
        )
    )

    codes = (
        await db_execute_all(
            sa.select(
                OAuthLoginAttempt.provider_id,
                OAuthLoginAttempt.failure_code,
                sa.func.count().label("cnt"),
            )
            .where(
                OAuthLoginAttempt.created_at >= since,
                OAuthLoginAttempt.failure_code.is_not(None),
            )
            .group_by(OAuthLoginAttempt.provider_id, OAuthLoginAttempt.failure_code),
            session_factory,
        )
    )

    provider_rows = (
        await db_execute_all(
            sa.select(OAuthProvider.id, OAuthProvider.code).where(
                OAuthProvider.enabled == 1
            ),
            session_factory,
        )
    )
    codes_by_provider = {row.id: row.code for row in provider_rows}
    # 每个供应商取「出现次数最多的失败码」作为主因。
    failure_counts: dict[int, dict[str, int]] = {}
    for row in codes:
        bucket = failure_counts.setdefault(row.provider_id, {})
        bucket[row.failure_code] = int(row.cnt or 0)
    top_failure: dict[int, str] = {}
    for provider_id, bucket in failure_counts.items():
        top_failure[provider_id] = max(bucket.items(), key=lambda kv: kv[1])[0]

    out: list[ProviderHealth] = []
    for row in rows:
        attempts = int(row.total or 0)
        failures = int(row.failed or 0)
        out.append(
            ProviderHealth(
                provider_code=codes_by_provider.get(row.provider_id, str(row.provider_id)),
                attempts=attempts,
                failures=failures,
                failure_ratio=(failures / attempts) if attempts else 0.0,
                top_failure=top_failure.get(row.provider_id, ""),
            )
        )
    out.sort(key=lambda item: (-item.failures, item.provider_code))
    return out


def _default_session_factory():
    """默认查主库（客户端应用）的会话工厂。"""
    from app.config.db import SessionLocal

    return SessionLocal


async def db_execute_all(stmt, session_factory=None) -> list:
    """在独立会话里执行一条查询并取回全部行。

    监控与清理是**后台任务**，没有请求上下文带来的会话，所以自己开一个。
    """
    factory = session_factory or _default_session_factory()
    async with factory() as db:
        result = await db.execute(stmt)
        return list(result.all())


async def cleanup_expired_oauth_attempts(session_factory=None) -> int:
    """删除一批过期登录事务。

    过期判定取「创建时间早于保留期」而不是「``expires_at`` 已过」：后者会把
    还在窗口内的失败记录一起删掉，限频计数会突然掉一截。

    ``session_factory`` 用于指定清哪个库：客户端应用清主库，管理端清管理库。
    """
    if not settings.oauth_attempt_cleanup_enabled:
        return 0
    try:
        factory = session_factory or _default_session_factory()

        cutoff = utcnow() - timedelta(days=max(1, settings.oauth_attempt_retention_days))
        limit = max(1, settings.oauth_attempt_cleanup_scan_limit)
        async with factory() as db:
            ids = list(
                await db.scalars(
                    sa.select(OAuthLoginAttempt.id)
                    .where(OAuthLoginAttempt.created_at < cutoff)
                    .order_by(OAuthLoginAttempt.id)
                    .limit(limit)
                )
            )
            if ids:
                await db.execute(
                    sa.delete(OAuthLoginAttempt).where(
                        OAuthLoginAttempt.id.in_(ids)
                    )
                )
                await db.commit()
        if ids:
            logger.info("已清理 %s 条过期 OAuth 登录事务", len(ids))
        return len(ids)
    except Exception:
        logger.exception("清理过期 OAuth 登录事务失败（下一周期继续）")
        return 0


async def report_provider_health(session_factory=None) -> list[ProviderHealth]:
    """输出 provider 登录健康状况，失败率超阈值时告警。

    每次调用都会打一行 INFO 的观测日志（谁在失败、失败多少）；超阈值再补一行
    WARNING——运维从日志里就能看出「是哪个 IdP 在坏」。
    """
    try:
        health = await provider_health(session_factory)
    except Exception:
        logger.exception("统计 OAuth provider 健康状况失败")
        return []
    for item in health:
        logger.info(
            "OAuth provider 登录状况：provider=%s 窗口内事务 %s 失败 %s 失败率 %.2f 主因 %s",
            item.provider_code,
            item.attempts,
            item.failures,
            item.failure_ratio,
            item.top_failure or "-",
        )
        if item.alerting:
            logger.warning(
                "OAuth provider 登录失败率超阈值：provider=%s 失败率 %.2f（%s/%s）主因 %s",
                item.provider_code,
                item.failure_ratio,
                item.failures,
                item.attempts,
                item.top_failure or "-",
            )
    return health


def start_attempt_cleanup(session_factory=None) -> None:
    """启动登录事务的周期清理与 provider 健康巡检。

    ``session_factory`` 决定清哪个库：客户端应用传主库的，管理端传管理库的。
    两个进程各清各的库——管理端与客户端的 ``oauth_login_attempt`` 是两张表。
    """
    global _cleanup_task

    if not settings.oauth_attempt_cleanup_enabled:
        return
    if _cleanup_task is not None and not _cleanup_task.done():
        return
    interval = max(1.0, settings.oauth_attempt_cleanup_interval_sec)
    _cleanup_task = asyncio.create_task(_cleanup_loop(interval, session_factory))
    logger.info("OAuth 登录事务清理已启动，间隔 %.1f 秒", interval)


async def _cleanup_loop(interval: float, session_factory=None) -> None:
    """周期清理 + 健康巡检；单轮失败不影响后续轮次。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await cleanup_expired_oauth_attempts(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OAuth 登录事务清理巡检失败（下一周期继续）")
        try:
            await report_provider_health(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OAuth provider 健康巡检失败（下一周期继续）")


async def stop_attempt_cleanup() -> None:
    """停止周期清理任务并等待其退出。"""
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
