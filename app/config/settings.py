"""应用配置（pydantic-settings，全部可由环境变量 / .env 覆盖）。"""

from __future__ import annotations

from functools import lru_cache

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
    # 生产建议：mysql+asyncmy://user:pass@host:3306/agentdb?charset=utf8mb4
    # 本地开发可用 sqlite+aiosqlite:///./web_agent.db
    database_url: str = "sqlite+aiosqlite:///./web_agent.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800
    db_echo: bool = False

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

    # ---- LLM ----
    openai_timeout: float = 120.0

    # ---- 演示/种子数据用的模型（scripts/seed.py 读取）----
    # 统一走 OpenAI 兼容协议，换供应商只改这三个值，不动代码。
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M2.7"
    minimax_api_key: str = ""

    # ---- 事件归档 ----
    run_event_retention_days: int = 7  # 设计 §4.2 清理策略


@lru_cache
def get_settings() -> Settings:
    """构造并缓存唯一的 Settings 实例。

    ``lru_cache`` 保证全进程只读一次 .env/环境变量：既避免每次 import/调用
    重解析，也避免测试里改环境变量后出现「两份配置」。要改配置请改环境变量
    并重启进程，而不是指望重新调用本函数。
    """
    return Settings()


settings = get_settings()
