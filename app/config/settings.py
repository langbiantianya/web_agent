"""应用配置（pydantic-settings，全部可由环境变量 / .env 覆盖）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 基础 ----
    app_name: str = "web-agent"
    debug: bool = False

    # ---- 数据库 ----
    # 三种方言都支持，靠 DATABASE_URL 里的驱动名区分：
    #   MySQL      mysql+asyncmy://user:pass@127.0.0.1:3306/agentdb?charset=utf8mb4
    #   PostgreSQL postgresql+psycopg://user:pass@127.0.0.1:5432/agentdb
    #   SQLite     sqlite+aiosqlite:///./web_agent.db（仅开发）
    database_url: str = "sqlite+aiosqlite:///./web_agent.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800
    db_echo: bool = False
    # PostgreSQL 的**会话时区**。PG 的 ``now()`` 返回带时区的时间，落到
    # ``TIMESTAMP``（无时区）列时按**会话时区**折算——不钉成 UTC，中国本机写的
    # 时间戳会整整差 8 小时。它通过 libpq 的 ``options`` 启动参数下发：那是
    # **连接建立时**生效的，不受事务回滚影响（PG 里裸 ``SET`` 是事务性的，
    # ROLLBACK 会把它撤销，见 app/config/db.py 的说明）。
    # 置空则不下发该参数（自己保证服务器时区是 UTC 时才需要）。
    db_pg_timezone: str = "UTC"

    # ---- 安全 ----
    # 用于 api_key 的 Fernet 加解密 + SSE ticket 签发。
    # 生产环境必须通过环境变量 SECRET_KEY 覆盖（可用 `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` 生成）。
    secret_key: str = "dev-insecure-secret-key-change-me-in-production-0123456789"
    sse_ticket_ttl: int = 60  # 秒。设计文档 §5.1 写 5 分钟，§8 表格写 60s，此处取更严格的 60s。

    # ---- 事件流 ----
    event_batch_size: int = 20       # 累积多少条 flush 一次（设计 §4.5）
    event_flush_interval: float = 0.05  # 或每 50ms flush 一次
    sse_ping_interval: int = 15      # 心跳间隔秒数（设计 §5.1）
    sse_replay_gap_threshold: int = 500  # 回放缺口超过该值视为事件已归档
    # 实时流空闲多久后回查一次 run 状态。兜住「重连时 run 刚好结束、事件还在
    # 批量队列里没落库」这种窗口——没有它，连接会一直等下去。
    sse_idle_recheck: float = 2.0

    # ---- 编排 ----
    context_max_tokens: int = 6000   # 上下文装配预算（设计 §4.4）
    context_max_messages: int = 50
    node_max_retries: int = 2        # 失败重试次数，指数退避（设计 §3.3）
    node_retry_base_delay: float = 0.5

    # ---- 工具调用（V3.2）----
    # 一次节点执行里「模型→工具→模型」最多来回几轮。超了就判定模型在工具上
    # 陷住了，直接失败——继续下去只会白烧 token，而重试同一个节点大概率还是
    # 同样地陷住。
    tool_max_rounds: int = 5
    # 工具结果回灌给模型时的最大字符数：太长会挤爆下一轮的 prompt。
    tool_result_max_chars: int = 4000

    # ---- LLM ----
    openai_timeout: float = 120.0

    # ---- 演示/种子数据用的模型（scripts/seed.py 读取）----
    # 统一走 OpenAI 兼容协议，换供应商只改这三个值，不动代码。
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M2.7"
    minimax_api_key: str = ""

    # ---- 事件归档 ----
    run_event_retention_days: int = 7  # 设计 §4.2 清理策略

    # ---- 审批暂停点（V3.1）----
    # LangGraph checkpointer 存的是「图暂停在哪、状态是什么」，**不存对外事件**
    # ——事件仍然只由 run_event + Redis Stream 负责，两者不冲突。
    # 开了 Redis 就用 Redis saver（多实例共享同一个暂停点），否则用本地 SQLite
    # 文件（单实例开发够用，且不引入新的外部依赖）。
    checkpoint_enabled: bool = True
    # 单实例开发用的本地 SQLite 文件路径。**相对路径按项目根解析**（不是按进程
    # 工作目录）——否则用不同的 WORKDIR 启动两个实例会各写各的 checkpoints.db，
    # 审批暂停点互不可见，run 就永远卡在 waiting。与下面的 sensitive_model_path
    # 同一个口径。
    checkpoint_sqlite_path: str = "./checkpoints.db"
    # 审批决策的等待上限（秒）。**当前未启用**（设计文档 §16.10）：超时算通过
    # 还是算驳回尚未定，语义没定就不能自动收尾——那等于替审批人做决定。
    # 保留这一项是为了把「将来启用时需要什么」写下来，不是说它现在生效。
    approval_timeout: int = 86400

    # ---- Redis（V2 多实例）----
    # 关：走进程内总线 + MySQL 回放（V1 单实例行为，现有测试全部依赖它）。
    # 开：事件走 Redis Stream（回放源）与 Pub/Sub（实时），可多实例水平扩容。
    #     开启后**没有降级路径**——Redis 不可用时服务直接启动失败。这是刻意的：
    #     静默退回进程内总线会造成「看起来能跑、实际跨实例收不到事件」的分裂大脑，
    #     比启动失败难查得多。单实例开发请显式关掉本项。
    redis_enabled: bool = False
    redis_url: str = "redis://192.168.1.5:6379/0"
    # 所有键与频道的统一前缀。多套环境共享同一个 Redis 时用它做隔离。
    redis_key_prefix: str = "webagent"
    # 每个 run 的事件 Stream 保留条数上限（近似裁剪）。Stream 是**回放源**，
    # 长期归档仍在 MySQL，所以只需保留够覆盖「断线重连 + 事件清理周期」的量。
    redis_stream_maxlen: int = 20000
    # run 活动标记的 TTL（秒）。生成方需周期性续期；TTL 到期即视为该 run 已无主，
    # 启动恢复据此判断「能不能把这条 running run 标成 error」。
    redis_activity_ttl: int = 120
    # Redis 订阅的轮询间隔（秒）。刻意与 ``sse_idle_recheck`` 分开：后者是 SSE
    # 「队列空了要不要回查 run 状态」的兜底周期，和订阅任务的收尾速度无关。
    # 退订时只置停止标志、让订阅任务在下一个周期自行退出（直接 cancel 会打断
    # 正在进行的 Redis 读，让连接锁状态不一致，后续关闭操作会永久挂起），
    # 所以这个值直接决定退订的快慢，1 秒是个平衡点。
    redis_poll_interval: float = 1.0
    # 单次 Redis 读写的**超时**（秒）。刻意与 ``socket_connect_timeout`` 分开：
    # 建连超时只管「连不连得上」，连上之后的读**默认没有上限**——对端只要不
    # 回包，这条协程就永远挂着。实测踩过：Redis 所在网段抖动时，XADD / PUBLISH
    # 里的一个读把整个生成流程冻住，外部看就是「服务不响应了」。
    # 5 秒对局域网里的 Redis 绰绰有余，而挂死的代价高得多。
    # 注意不影响 Pub/Sub：``get_message(timeout=...)`` 自己带超时，
    # ``parse_response(block=True)`` 显式传 ``math.inf``，都不受这个值影响。
    redis_socket_timeout: float = 5.0

    # ---- 敏感词前置过滤（V3.4）----
    # 总开关，默认开。这是「提示词进 LLM 之前」的那道前置，关掉它等于没有。
    # 显式关掉是有记录、可审计的：配置里留痕，启动日志里也有一行说明。
    sensitive_enabled: bool = True
    # 判定阈值。留 0 表示用**模型自带**的推荐阈值——那是训练时按「误报率不超过
    # 目标」挑出来的，比手填一个数靠谱。显式给值则覆盖它：调高更严（漏得多），
    # 调低更松（误杀多）。
    sensitive_threshold: float = 0.0
    # 模型文件路径。默认指向仓库里那份，路径按**项目根**解析而不是相对工作
    # 目录——服务从哪个目录启动都能找到模型，少一类「本地能跑、部署找不到」。
    sensitive_model_path: str = str(
        Path(__file__).resolve().parents[2] / "app" / "core" / "sensitive_model.npz"
    )
    # 滑窗参数。0 表示用模型 meta 里记录的值。重训换了窗口，推理必须跟着换，
    # 所以默认值来自模型而不是写死在代码里；这两项只给「临时调参、不重训」用。
    sensitive_window: int = 0
    sensitive_stride: int = 0
    # 单条提示词最多扫描多少字符。超长的只扫前面这段——敏感内容几乎总在开头，
    # 而这一步是为了**控住资源占用**。
    sensitive_max_chars: int = 4000


@lru_cache
def get_settings() -> Settings:
    """构造并缓存唯一的 Settings 实例。

    ``lru_cache`` 保证全进程只读一次 .env/环境变量：既避免每次 import/调用
    重解析，也避免测试里改环境变量后出现「两份配置」。要改配置请改环境变量
    并重启进程，而不是指望重新调用本函数。
    """
    return Settings()


settings = get_settings()
