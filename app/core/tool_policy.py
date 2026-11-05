"""工具级权限、并发和超时策略。

Agent 的 ``tool_policy`` 是 ``{工具名: 策略}``。未配置的工具使用默认值；显式
``enabled=false`` 的工具既不展开给模型，也不能通过伪造 tool_call 执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.errors import BadRequestError

DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_TIMEOUT_SEC = 60.0


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """单个工具的执行策略。"""

    enabled: bool = True
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    timeout_sec: float = DEFAULT_TIMEOUT_SEC


def normalize_tool_policy(
    raw: dict[str, Any] | None,
    *,
    known_tools: set[str],
) -> dict[str, dict] | None:
    """校验并规范化工具策略。

    Args:
        raw: Pydantic 解析后的策略字典。
        known_tools: 当前 Agent 已授权且服务端已知的工具名。

    Returns:
        可写入 JSON 列的字典；空配置返回 ``None``。

    Raises:
        BadRequestError: 工具名未授权，或并发/超时数值非法。
    """
    if not raw:
        return None
    out: dict[str, dict] = {}
    for name, value in raw.items():
        if name not in known_tools:
            raise BadRequestError(f"工具策略引用了未授权工具：{name}")
        if hasattr(value, "model_dump"):
            item = value.model_dump(exclude_none=True)
        elif isinstance(value, dict):
            item = dict(value)
        else:
            raise BadRequestError(f"工具 {name} 的策略必须是对象")
        enabled = bool(item.get("enabled", True))
        try:
            max_concurrency = int(
                item.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)
            )
            timeout_sec = float(item.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
        except (TypeError, ValueError) as exc:
            raise BadRequestError(f"工具 {name} 的并发或超时必须是数字") from exc
        if not 1 <= max_concurrency <= 16:
            raise BadRequestError(f"工具 {name} 的 max_concurrency 必须在 1..16")
        if not 0.1 <= timeout_sec <= 600:
            raise BadRequestError(f"工具 {name} 的 timeout_sec 必须在 0.1..600")
        out[name] = {
            "enabled": enabled,
            "max_concurrency": max_concurrency,
            "timeout_sec": timeout_sec,
        }
    return out or None


def policy_for(raw: dict[str, Any] | None, tool_name: str) -> ToolPolicy:
    """读取工具策略，脏数据回退默认值。"""
    item = (raw or {}).get(tool_name)
    if not isinstance(item, dict):
        return ToolPolicy()
    try:
        concurrency = int(
            item.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)
        )
    except (TypeError, ValueError):
        concurrency = DEFAULT_MAX_CONCURRENCY
    try:
        timeout = float(item.get("timeout_sec", DEFAULT_TIMEOUT_SEC))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SEC
    return ToolPolicy(
        enabled=bool(item.get("enabled", True)),
        max_concurrency=max(1, min(concurrency, 16)),
        timeout_sec=max(0.1, min(timeout, 600.0)),
    )


def enabled_tool_names(
    names: list[str] | None, raw_policy: dict[str, Any] | None
) -> list[str] | None:
    """过滤白名单中被策略禁用的工具。"""
    if names is None:
        return None
    return [name for name in names if policy_for(raw_policy, name).enabled]
