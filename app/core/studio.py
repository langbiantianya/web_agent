"""studio 的 gRPC 客户端（设计文档 §22.8、§22.8.5）。

## 这一层只做一件事：把「一次脚本执行」变成一次 RPC

调用侧的职责边界很窄：

1. **把脚本正文读出来**（从数据库，不从文件系统——技能已经不住在磁盘上了）；
2. **把参数校验干净**（路径格式、argv 形态）——校验必须在**请求发出去之前**，
   studio 收到的永远是已校验过的内容；
3. **给一个 request_id**，让 studio 的去重窗口有机会生效；
4. **把 studio 的回答翻译成给模型看的文本**。

## 几个必须钉死的口径

| 关注点 | 口径 |
| --- | --- |
| 超时 | 模型能请求的时限**不超过** ``studio_exec_max_timeout_ms``——上限写死在配置里，模型改不了 |
| gRPC deadline | ``timeout_ms + 2 秒``宽限。先到哪个算哪个 |
| 脚本 exit 1 | **不是**错误。是 ``COMPLETED`` + ``exit_code=1``，照样回灌给模型（§17.5） |
| studio 挂了 | ``UNAVAILABLE`` → 抛 :class:`StudioUnavailableError`，由上层回灌给模型，**不失败节点** |
| 调用超时 | ``DEADLINE_EXCEEDED`` → 抛 :class:`StudioTimeoutError`；但 studio 侧有自设时限兜底（§22.8.5） |

**gRPC 成功 ≠ 脚本成功。** 这是本契约最重要的一条语义（skill-service §4.1）：
gRPC 层面成功只说明「studio 正常工作」，脚本成功与否由 ``status`` 与 ``exit_code``
表达。用 gRPC 错误码去报「脚本失败」的话，调用方只能当成服务不可用处理——语义完全
错了，模型也拿不到 stderr 的细节。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import grpc

from webagent.studio.v1 import studio_pb2, studio_pb2_grpc

from app.config.settings import settings
from app.core.studio_context import current_exec_context

logger = logging.getLogger(__name__)

# gRPC deadline 的宽限：脚本时限到了之后，还要留一点时间让 studio 回话、
# 让在途的响应走完网络。2 秒是经验值——太短会把「脚本刚好跑完」误判成超时。
DEADLINE_GRACE_SEC = 2.0

# 档位名（skill-service §2.4）。请求里带、响应里回显，调用方据此对账防降级。
PROFILE_FAST = "fast"
PROFILE_ISOLATED = "isolated"

# 档位强弱：只能升、不能降。响应里的实际档位低于请求档位 = 静默降级。
_PROFILE_RANK = {PROFILE_FAST: 1, PROFILE_ISOLATED: 2}

_PROFILE_TO_PROTO = {
    PROFILE_FAST: studio_pb2.EXEC_PROFILE_FAST,
    PROFILE_ISOLATED: studio_pb2.EXEC_PROFILE_ISOLATED,
}

_PROTO_TO_PROFILE = {
    studio_pb2.EXEC_PROFILE_FAST: PROFILE_FAST,
    studio_pb2.EXEC_PROFILE_ISOLATED: PROFILE_ISOLATED,
}

# gRPC 的负载均衡策略（skill-service §13.4）。默认 ``pick_first`` 只会使用
# 第一个解析结果；当同一个名字解析出多个后端地址时，长连接不会自动分摊。
# ``dns:///`` 前缀让 gRPC 获取全部 A 记录，``round_robin`` 再把请求分给它们。
LB_POLICY_DEFAULT = "round_robin"

# proto 枚举 → 我们的状态名。
_STATUS_NAMES: dict[int, str] = {
    studio_pb2.EXEC_STATUS_UNSPECIFIED: "unspecified",
    studio_pb2.EXEC_STATUS_COMPLETED: "completed",
    studio_pb2.EXEC_STATUS_FAILED: "failed",
    studio_pb2.EXEC_STATUS_TIMEOUT: "timeout",
    studio_pb2.EXEC_STATUS_LIMITED: "limited",
    studio_pb2.EXEC_STATUS_CANCELLED: "cancelled",
}


class StudioError(Exception):
    """studio 调用失败——服务不可用、参数非法、队列满等。

    ``code`` 是机器可读的失败码（``capability_missing`` / ``sandbox_capacity``
    / ``image_unknown`` …）。gRPC 错误把它放在 details 的 ``code:`` 前缀里；
    响应级错误直接读 ``ExecResponse.error_code``。调用方按它分支，不靠匹配人话。
    """

    def __init__(self, message: str, code: str = "") -> None:
        """记住说明与失败码。

        Args:
            message: 给人看的说明。
            code: 机器可读的失败码；没有就是空串。
        """
        super().__init__(message)
        self.code = code


class StudioUnavailableError(StudioError):
    """连不上 studio（``UNAVAILABLE``）。"""


class StudioTimeoutError(StudioError):
    """调用超时（``DEADLINE_EXCEEDED``）。"""


class StudioStartupError(StudioError):
    """studio 不可达且要求必须可用——服务应当**启动失败**（§22.8.6）。"""


class StudioDowngradeError(StudioError):
    """响应里的实际档位低于请求档位——**静默降级**，拒绝该结果。

    这是「不悄悄退回 fast」唯一可验证的落点：不靠 studio 自觉，靠调用方拿
    响应里的 ``profile`` 对账。
    """


@dataclass(frozen=True, slots=True)
class StudioProfile:
    """一个档位现在能不能用（``HealthResponse.ProfileInfo``）。"""

    name: str
    ready: bool
    max_concurrency: int = 0
    queue_depth: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StudioImage:
    """isolated 可用的镜像目录项（``HealthResponse.ImageInfo``）。"""

    name: str
    languages: tuple[str, ...] = ()
    digest: str = ""


@dataclass(frozen=True, slots=True)
class StudioHealth:
    """studio 的健康信息。"""

    ready: bool
    version: str
    languages: tuple[str, ...]
    max_concurrency: int
    profile: str
    profiles: tuple[StudioProfile, ...] = ()
    images: tuple[StudioImage, ...] = ()
    isolated_startup_budget_ms: int = 0

    def profile_info(self, name: str) -> StudioProfile | None:
        """按档位名取能力。

        Args:
            name: ``fast`` / ``isolated``。

        Returns:
            该档位的能力；没上报时返回 ``None``（老 studio）。
        """
        for item in self.profiles:
            if item.name == name:
                return item
        return None

    @property
    def supports_isolated(self) -> bool:
        """本实例能不能跑 isolated 档。

        老 studio 没有 ``profiles`` 字段——**视为没有**，不能因为「字段缺失」
        就当成兼容而放行。
        """
        info = self.profile_info(PROFILE_ISOLATED)
        return bool(info and info.ready)

    def image(self, name: str) -> StudioImage | None:
        """按目录名取镜像信息。"""
        for item in self.images:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True, slots=True)
class ExecResult:
    """一次脚本执行的结果。

    ``status`` 取值见 :data:`_STATUS_NAMES`。``exit_code`` 只有 ``completed``
    时有意义；其余状态是 ``-1``。
    """

    status: str
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int
    error: str
    stdout_digest: str
    profile: str = ""
    startup_ms: int = 0
    error_code: str = ""

    @property
    def ok(self) -> bool:
        """脚本是否**成功**跑完了。

        「gRPC 成功」不等于「脚本成功」：脚本自己 ``exit 1`` 是 ``completed``
        + ``exit_code=1``，对它来说这次执行是失败的（模型看到 stderr 会自己
        纠正）。但它**不是**服务故障。
        """
        return self.status == "completed" and self.exit_code == 0


def effective_timeout_ms(requested: int | None = None) -> int:
    """算出这次执行真正生效的时限。

    模型请求的时限是**上限之内的请求**，不是上限本身——上限写死在配置里，
    模型改不了（§22.8.8）。

    Args:
        requested: 模型请求的时限（毫秒）；``None`` 表示用默认时限。

    Returns:
        生效的时限（毫秒）。
    """
    base = settings.studio_exec_timeout_ms if requested is None else int(requested)
    return max(1, min(int(base), int(settings.studio_exec_max_timeout_ms)))


def _version_tuple(value: str) -> tuple[int, int, int]:
    """把 ``2.0.0`` / ``2.0`` 这类版本串解析成可比较的元组。

    Args:
        value: 版本串。

    Returns:
        ``(major, minor, patch)``；解析不了返回 ``(0, 0, 0)``。
    """
    cleaned = (value or "").strip().split("-", 1)[0]
    parts: list[int] = []
    for item in cleaned.split(".")[:3]:
        try:
            parts.append(int(item))
        except ValueError:
            return (0, 0, 0)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def version_at_least(actual: str, minimum: str) -> bool:
    """``actual >= minimum``。

    Args:
        actual: 实际版本。
        minimum: 最低版本。

    Returns:
        是否满足下限。
    """
    return _version_tuple(actual) >= _version_tuple(minimum)


def channel_target(addr: str) -> str:
    """把配置里的地址转成 gRPC 的 target。

    ``dns:///`` 前缀让 gRPC 用 DNS 解析器拿到**全部** A 记录，而不是只拿第一个。
    轮询策略要配合它才有意义（skill-service §13.4）。

    已经带 scheme 的地址（``unix:`` / ``xds:`` / ``dns:///`` …）原样返回——
    那种场景下解析器由用户自己指定。

    Args:
        addr: 配置里的地址，如 ``studio:9000``。

    Returns:
        gRPC target。
    """
    if "://" in addr:
        return addr
    return f"dns:///{addr}"


def _lb_policy() -> str:
    """取要用的负载均衡策略名。"""
    return (settings.studio_exec_lb_policy or LB_POLICY_DEFAULT).strip() or (
        LB_POLICY_DEFAULT
    )


class StudioClient:
    """到一台 studio 的 gRPC 客户端。

    通道是**懒创建**的——构造时不碰网络，第一次调用时才建连。这样「配置了地址
    但 studio 暂时没起来」不会让 web_agent 自己也起不来（真起不来的判定在
    启动期的健康检查里，§22.8.6）。
    """

    def __init__(self, addr: str) -> None:
        """记住 studio 的地址。

        Args:
            addr: gRPC 地址，如 ``studio:9000``。
        """
        self.addr = addr
        self._channel: grpc.aio.Channel | None = None
        self._stub: studio_pb2_grpc.StudioStub | None = None
        # 最近一次探活的结果。调用方据此校验「这个脚本的语言本实例能不能跑」
        # （skill-service §10.2 的「写的时候严格」）。探活之前是 None——那表示
        # 还没探过，不做这道校验。
        self.health_info: StudioHealth | None = None
        # 上次探活的时刻（``time.monotonic``）。能力缓存按 TTL 刷新——studio
        # 滚动升级后镜像/档位可能变化，一直用启动时那份会误判。
        self.health_fetched_at: float | None = None

    async def _get_stub(self) -> studio_pb2_grpc.StudioStub:
        """取（或建）桩对象。

        Returns:
            ``StudioStub``。
        """
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(
                channel_target(self.addr),
                options=[("grpc.lb_policy_name", _lb_policy())],
            )
            self._stub = studio_pb2_grpc.StudioStub(self._channel)
        return self._stub

    async def health(self, timeout: float = 5.0) -> StudioHealth:
        """探活。

        必须**便宜**：不跑脚本、不读文件（skill-service §13.3 的探针要求）。

        Args:
            timeout: 调用超时（秒）。

        Returns:
            :class:`StudioHealth`。

        Raises:
            StudioUnavailableError: 连不上。
            StudioTimeoutError: 探活超时。
            StudioError: 其它 gRPC 错误。
        """
        stub = await self._get_stub()
        try:
            response = await stub.Health(
                studio_pb2.HealthRequest(), timeout=timeout
            )
        except grpc.aio.AioRpcError as exc:
            raise _map_rpc_error(exc, self.addr) from exc
        health = StudioHealth(
            ready=bool(response.ready),
            version=response.version,
            languages=tuple(info.name for info in response.language_info),
            max_concurrency=int(response.max_concurrency),
            profile=response.profile,
            profiles=tuple(
                StudioProfile(
                    name=item.name,
                    ready=bool(item.ready),
                    max_concurrency=int(item.max_concurrency),
                    queue_depth=int(item.queue_depth),
                    detail=item.detail,
                )
                for item in response.profiles
            ),
            images=tuple(
                StudioImage(
                    name=item.name,
                    languages=tuple(item.languages),
                    digest=item.digest,
                )
                for item in response.images
            ),
            isolated_startup_budget_ms=int(response.isolated_startup_budget_ms),
        )
        self.health_info = health
        self.health_fetched_at = time.monotonic()
        return health

    async def ensure_health(self, timeout: float = 5.0) -> StudioHealth | None:
        """按 TTL 拿一份可用的能力快照。

        为什么不在每次执行前都探活：fast 档不值得多一个 RTT。调用方只在
        「需要判断 isolated 能不能跑」时用它；TTL 内复用，过期才刷新。

        Args:
            timeout: 探活超时（秒）。

        Returns:
            能力快照；一次都没探到过时返回 ``None``。刷新失败时退回上一次的
            快照（真正的执行失败会由 Exec 报出来）。
        """
        ttl = float(settings.studio_exec_health_ttl_sec or 0)
        if (
            self.health_info is not None
            and self.health_fetched_at is not None
            and ttl > 0
            and time.monotonic() - self.health_fetched_at < ttl
        ):
            return self.health_info
        try:
            return await self.health(timeout=timeout)
        except StudioError as exc:
            logger.warning("刷新 studio 能力失败（%s）：%s", self.addr, exc)
            return self.health_info

    async def exec_script(
        self,
        *,
        skill: str,
        script: str,
        language: str,
        content: str,
        argv: list[str] | None = None,
        stdin: str = "",
        timeout_ms: int | None = None,
        profile: str = "",
        image: str = "",
        run_id: str = "",
        node_id: str = "",
        request_id: str = "",
    ) -> ExecResult:
        """执行一个脚本。

        Args:
            skill: 技能 code（审计用）。
            script: 相对技能根的脚本路径（审计用）。
            language: ``python`` / ``bash`` / ``node``。
            content: 脚本正文。
            argv: 传给脚本的参数。**绝不拼成 shell 字符串**（skill-service §9.1）。
            stdin: 标准输入（文本）。
            timeout_ms: 执行时限（毫秒）；超过配置上限会被压到上限。
            profile: 请求的档位（``fast`` / ``isolated``）；空 = 沿用服务端路由。
            image: 镜像**目录名**（不是完整 ref）；空 = 默认镜像。
            run_id: 关联的 run id（审计）。
            node_id: 关联的节点 id（审计）。
            request_id: 追踪键。同键在 studio 的去重窗口内不重复执行。

        Returns:
            :class:`ExecResult`。

        Raises:
            StudioDowngradeError: 响应里的实际档位低于请求档位（静默降级）。
            StudioUnavailableError: 连不上 studio。
            StudioTimeoutError: 调用超时。
            StudioError: 其它 gRPC 错误（参数非法、队列满、运行时缺失等）。
        """
        timeout = effective_timeout_ms(timeout_ms)
        deadline = timeout / 1000.0 + DEADLINE_GRACE_SEC
        stub = await self._get_stub()
        ctx = current_exec_context()
        request = studio_pb2.ExecRequest(
            skill=skill,
            script=script,
            language=language,
            content=content,
            argv=list(argv or []),
            stdin=stdin,
            timeout_ms=timeout,
            run_id=run_id,
            node_id=node_id,
            request_id=request_id,
            profile=_PROFILE_TO_PROTO.get(
                profile, studio_pb2.EXEC_PROFILE_UNSPECIFIED
            ),
            image=image,
            traceparent=(ctx.traceparent if ctx else ""),
        )
        try:
            response = await stub.Exec(request, timeout=deadline)
        except grpc.aio.AioRpcError as exc:
            raise _map_rpc_error(exc, self.addr) from exc
        result = _to_result(response)
        # 对账：实际档位不能低于请求档位。老 studio 不回显 → 实际档位是空串，
        # 排在 fast 之前，同样会被拒——**不能因为字段缺失就当成兼容**。
        if profile and _PROFILE_RANK.get(result.profile, 0) < _PROFILE_RANK.get(
            profile, 0
        ):
            raise StudioDowngradeError(
                f"studio 实际以 {result.profile or '未知'} 档执行了要求 {profile} "
                "档的技能，已拒绝该结果",
                code="profile_downgraded",
            )
        return result

    async def close(self) -> None:
        """关掉通道。"""
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception:  # pragma: no cover - 收尾不该抛
                logger.debug("关闭 studio 通道失败", exc_info=True)
        self._channel = None
        self._stub = None


def _to_result(response: Any) -> ExecResult:
    """把 proto 响应转成 :class:`ExecResult`。

    Args:
        response: ``ExecResponse``。

    Returns:
        :class:`ExecResult`。
    """
    return ExecResult(
        status=_STATUS_NAMES.get(int(response.status), "unspecified"),
        exit_code=int(response.exit_code),
        stdout=response.stdout,
        stderr=response.stderr,
        truncated=bool(response.truncated),
        duration_ms=int(response.duration_ms),
        error=response.error,
        stdout_digest=response.stdout_digest,
        profile=_PROTO_TO_PROFILE.get(int(response.profile), ""),
        startup_ms=int(response.startup_ms),
        error_code=response.error_code,
    )


def _split_error_code(detail: str) -> tuple[str, str]:
    """从 gRPC details 里拆出 ``code: 人话`` 前缀。

    Args:
        detail: gRPC 的 details 文本。

    Returns:
        ``(code, message)``；没有前缀时 code 是空串、message 是原文。
    """
    code, sep, rest = detail.partition(":")
    if sep and code and " " not in code and len(code) <= 48:
        return code.strip(), rest.strip()
    return "", detail


def _map_rpc_error(exc: grpc.aio.AioRpcError, addr: str) -> StudioError:
    """把 gRPC 错误翻译成我们自己的异常。

    Args:
        exc: gRPC 异常。
        addr: studio 地址（写进错误信息里，便于定位）。

    Returns:
        对应的 :class:`StudioError` 子类实例。
    """
    code = exc.code()
    raw_detail = exc.details() or ""
    error_code, detail = _split_error_code(raw_detail)
    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        return StudioTimeoutError(
            f"调用 studio 超时（{addr}）：{detail}", code=error_code or "deadline"
        )
    if code == grpc.StatusCode.UNAVAILABLE:
        return StudioUnavailableError(
            f"连不上 studio（{addr}）：{detail}", code=error_code or "unavailable"
        )
    if code == grpc.StatusCode.INVALID_ARGUMENT:
        return StudioError(
            f"studio 拒绝了参数（{addr}）：{detail}",
            code=error_code or "invalid_argument",
        )
    if code == grpc.StatusCode.FAILED_PRECONDITION:
        return StudioError(
            f"studio 不满足前置条件（{addr}）：{detail}",
            code=error_code or "failed_precondition",
        )
    if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
        return StudioError(
            f"studio 忙不过来（{addr}）：{detail}",
            code=error_code or "resource_exhausted",
        )
    return StudioError(
        f"studio 调用失败（{addr}，{code.name}）：{detail}",
        code=error_code or "internal",
    )


# ------------------------------------------------------------------ 全局单例


_client: StudioClient | None = None


def configure_studio(addr: str) -> StudioClient | None:
    """按地址配置全局客户端。

    地址为空 = **未启用脚本执行**（§22.8.8）——这时返回 ``None``，``skill_exec``
    不会注册。

    Args:
        addr: gRPC 地址；空串表示未启用。

    Returns:
        已配置的客户端；未启用时返回 ``None``。
    """
    global _client
    cleaned = (addr or "").strip()
    if not cleaned:
        _client = None
        return None
    if _client is None or _client.addr != cleaned:
        _client = StudioClient(cleaned)
    return _client


def get_studio_client() -> StudioClient | None:
    """全局客户端；未启用时返回 ``None``。"""
    return _client


def studio_configured() -> bool:
    """脚本执行**是否配置了**（地址非空）。"""
    return bool(settings.studio_exec_addr.strip())


async def init_studio(timeout: float = 5.0) -> bool:
    """启动期检查 studio，决定脚本执行能力是否启用（§22.8.6）。

    三种情形，**两件事**：

    | 情形 | 结果 |
    | --- | --- |
    | 地址为空 | 未启用（这不是「不可达」，是「压根没配」） |
    | 地址有、连上了 | 启用 |
    | 地址有、连不上 | ``studio_exec_required`` 为真 → **启动失败**；为假 → 未启用，只记一条警告 |

    为什么 studio 不可达要**启动失败**：studio 是部署的一个组成部分，它挂了说明
    部署不完整。让问题在部署时暴露，而不是在用户的一次 run 里暴露——那时用户看到
    的是「这个 Agent 好像少了个能力」，排查成本高得多。

    口径与 §20.7（敏感词模型缺失即启动失败）一致：宁可起不来，也不要「看起来
    能跑、行为已错」。

    Args:
        timeout: 探活超时（秒）。

    Returns:
        脚本执行能力是否启用。

    Raises:
        StudioStartupError: studio 连不上且 ``studio_exec_required`` 为真。
    """
    client = configure_studio(settings.studio_exec_addr)
    if client is None:
        logger.info("未配置 studio 地址，脚本执行能力未启用")
        return False
    try:
        health = await client.health(timeout=timeout)
    except StudioError as exc:
        if settings.studio_exec_required:
            raise StudioStartupError(
                f"studio 不可达（{client.addr}）：{exc}。"
                "确认 studio 已部署，或设 STUDIO_EXEC_REQUIRED=false 显式关掉脚本执行。"
            ) from exc
        logger.warning(
            "studio 不可达（%s），脚本执行能力未启用：%s", client.addr, exc
        )
        return False
    minimum = (settings.studio_exec_min_version or "").strip()
    if minimum and not version_at_least(health.version, minimum):
        message = (
            f"studio 版本过低（{client.addr}）：{health.version} < {minimum}。"
            "升级 studio，或把 STUDIO_EXEC_MIN_VERSION 调到实际版本。"
        )
        if settings.studio_exec_required:
            raise StudioStartupError(message)
        logger.warning("%s，脚本执行能力未启用", message)
        return False
    if not health.ready:
        detail = health.profile_info(PROFILE_ISOLATED)
        suffix = f"，isolated={detail.detail}" if detail and detail.detail else ""
        message = (
            f"studio 未就绪（{client.addr}）：profile={health.profile}{suffix}"
        )
        if settings.studio_exec_required:
            raise StudioStartupError(message)
        logger.warning("%s，脚本执行能力未启用", message)
        return False
    profile_desc = "、".join(
        f"{item.name}={'ready' if item.ready else 'not-ready'}"
        for item in health.profiles
    ) or f"{health.profile}（老 studio 未分级）"
    logger.info(
        "studio 就绪（%s）：版本 %s，运行时 %s，并发上限 %s，档位 %s",
        client.addr,
        health.version,
        "、".join(health.languages) or "（未声明）",
        health.max_concurrency,
        profile_desc,
    )
    return True


async def close_studio_client() -> None:
    """关掉全局客户端（进程退出 / 测试收尾时用）。"""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
