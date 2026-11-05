"""工具调用审计写入、脱敏与保留期清理（V4.3）。

审计采用 best-effort：工具结果先按原有路径回灌模型，审计写失败只记日志。
这样数据库临时抖动不会把一个已经成功执行的外部工具伪装成失败。敏感输出
被拦截时只记录状态和耗时，绝不保存命中正文。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

from app.config.settings import settings
from app.core.errors import SensitiveContentError, redact_error_message
from app.core.mcp.naming import is_mcp_name, parse_qualified_name
from app.models.base import utcnow

logger = logging.getLogger(__name__)

_cleanup_task: asyncio.Task | None = None

_SECRET_KEY_NAMES = {
    "accesstoken",
    "authorization",
    "bearer",
    "clientsecret",
    "cookie",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "setcookie",
    "token",
}


def _normalize_key(key: str) -> str:
    """把常见 key 命名统一成只含小写字母的形式。"""
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _is_secret_key(key: str) -> bool:
    """判断 JSON 字段名是否表示凭据。"""
    normalized = _normalize_key(key)
    return normalized in _SECRET_KEY_NAMES or normalized.endswith(
        ("apikey", "password", "secret", "token")
    )


def redact_audit_value(value: Any, *, key: str = "") -> tuple[Any, bool]:
    """递归脱敏审计 JSON，同时保留其结构。

    Args:
        value: 参数或任意 JSON 值。
        key: 当前值对应的字段名。

    Returns:
        ``(脱敏后的值, 是否发生脱敏)``。
    """
    if key and _is_secret_key(key):
        return "***", True
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        changed = False
        for item_key, item_value in value.items():
            text_key = str(item_key)
            safe_value, item_changed = redact_audit_value(
                item_value, key=text_key
            )
            redacted[text_key] = safe_value
            changed = changed or item_changed
        return redacted, changed
    if isinstance(value, (list, tuple)):
        redacted_list = []
        changed = False
        for item in value:
            safe_item, item_changed = redact_audit_value(item)
            redacted_list.append(safe_item)
            changed = changed or item_changed
        return redacted_list, changed
    if isinstance(value, str):
        safe_text = redact_error_message(value) or ""
        return safe_text, safe_text != value
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    text = str(value)
    safe_text = redact_error_message(text) or ""
    return safe_text, safe_text != text


def prepare_audit_arguments(arguments: str | dict | None) -> tuple[Any, bool, int]:
    """把工具参数规范化为可存 JSON，并标记是否经过脱敏。

    Returns:
        ``(可存 JSON, 是否发生脱敏, 原始字符数)``。解析失败时保留脱敏文本，
        不丢掉「调用方确实传了什么形状」这一事实。
    """
    if isinstance(arguments, str):
        raw = arguments
        try:
            parsed: Any = json.loads(arguments) if arguments.strip() else {}
        except (TypeError, ValueError):
            parsed = {"unparsed": arguments}
    elif arguments is None:
        raw = "{}"
        parsed = {}
    else:
        parsed = arguments
        try:
            raw = json.dumps(arguments, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw = str(arguments)
            parsed = {"unparsed": raw}

    safe_value, redacted = redact_audit_value(parsed)
    # 兜底处理 raw 中位于普通字符串里的凭据，例如工具参数是一整段日志。
    safe_raw = redact_error_message(raw) or ""
    if safe_raw != raw:
        redacted = True
    return safe_value, redacted, len(raw)


def prepare_audit_result(text: str | None) -> tuple[str | None, int, int, bool]:
    """脱敏并限制审计结果大小。

    Returns:
        ``(保存文本, 原始字符数, 保存字节数, 是否截断)``。字符数是执行器交回
        服务侧的文本长度；它可能已经被工具自身限制，因此不承诺无限归档。
    """
    if text is None:
        return None, 0, 0, False
    original_chars = len(text)
    safe_text = redact_error_message(text) or ""
    limit = max(0, settings.tool_audit_max_result_chars)
    truncated = len(safe_text) > limit
    stored = safe_text[:limit]
    return stored, original_chars, len(stored.encode("utf-8")), truncated


def classify_tool_source(tool_name: str) -> tuple[str, str | None]:
    """识别审计来源与 MCP 服务器 code。

    Returns:
        ``(source, server)``。Skill 管理工具标为 ``skill``，外部 MCP 工具标为
        ``mcp``，其余内置工具标为 ``builtin``。
    """
    if is_mcp_name(tool_name):
        try:
            server, _ = parse_qualified_name(tool_name)
        except ValueError:
            return "mcp", None
        return "mcp", server
    if tool_name in {"skill_exec", "skill_resource"}:
        return "skill", None
    return "builtin", None


async def record_tool_audit(
    *,
    tenant_id: str = "default",
    run_id: int,
    node_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: str | dict | None,
    ok: bool,
    output: str | None,
    duration_ms: int,
    failure_type: str | None = None,
) -> int | None:
    """写入一条独立、脱敏的工具调用审计。

    本函数不会向调用方抛异常：数据库不可用或单条写入失败时返回 ``None``，
    工具结果仍按原业务路径处理。敏感内容被拦截时即使调用方误传了正文，也只
    记录 ``sensitive_content`` 状态，不保存输出。

    Returns:
        新记录 id；审计关闭或写入失败时返回 ``None``。
    """
    if not settings.tool_audit_enabled:
        return None

    try:
        from app.config.db import SessionLocal
        from app.repo.tool_audit_repo import tool_audit_repo

        source, server = classify_tool_source(tool_name)
        safe_arguments, arguments_redacted, arguments_chars = (
            prepare_audit_arguments(arguments)
        )

        if failure_type == "sensitive_content":
            safe_result = None
            result_chars = 0
            result_bytes = 0
            result_truncated = False
        else:
            safe_result, result_chars, result_bytes, result_truncated = (
                prepare_audit_result(output)
            )

        if not ok and failure_type is None:
            failure_type = "tool_error"
        now = utcnow()
        expires_at = now + timedelta(days=max(1, settings.tool_audit_retention_days))
        async with SessionLocal() as db:
            row = await tool_audit_repo.create(
                db,
                tenant_id=(tenant_id or "default")[:64],
                run_id=run_id,
                node_id=node_id[:64],
                tool_call_id=(tool_call_id or "")[:128],
                tool_name=tool_name[:255],
                source=source,
                server=(server[:128] if server else None),
                arguments_json=safe_arguments,
                arguments_redacted=1 if arguments_redacted else 0,
                arguments_chars=arguments_chars,
                result_text=safe_result,
                result_chars=result_chars,
                result_bytes=result_bytes,
                result_truncated=1 if result_truncated else 0,
                ok=1 if ok else 0,
                failure_type=(failure_type[:64] if failure_type else None),
                duration_ms=max(0, duration_ms),
                expires_at=expires_at,
            )
        return int(row.id)
    except Exception:
        logger.exception(
            "写入工具审计失败（run=%s node=%s tool=%s），业务结果不受影响",
            run_id,
            node_id,
            tool_name,
        )
        return None


def failure_type_for_exception(exc: BaseException) -> str:
    """把未预期异常转成稳定、带长度上限的审计分类。"""
    if isinstance(exc, SensitiveContentError):
        return "sensitive_content"
    return f"exception:{type(exc).__name__}"[:64]


async def cleanup_expired_tool_audits() -> int:
    """删除一批过期审计记录。

    单次删除量由 ``tool_audit_cleanup_scan_limit`` 控制，避免小内存机器一次
    加载过多主键。调用方负责重复巡检。
    """
    if not settings.tool_audit_enabled or not settings.tool_audit_cleanup_enabled:
        return 0
    try:
        from app.config.db import SessionLocal
        from app.repo.tool_audit_repo import tool_audit_repo

        async with SessionLocal() as db:
            deleted = await tool_audit_repo.purge_expired(
                db,
                now=utcnow(),
                limit=max(1, settings.tool_audit_cleanup_scan_limit),
            )
        if deleted:
            logger.info("已清理 %s 条过期工具审计", deleted)
        return deleted
    except Exception:
        logger.exception("清理过期工具审计失败（下一周期继续）")
        return 0


def start_tool_audit_cleanup() -> None:
    """启动工具审计周期清理任务。"""
    global _cleanup_task

    if not settings.tool_audit_enabled or not settings.tool_audit_cleanup_enabled:
        return
    if _cleanup_task is not None and not _cleanup_task.done():
        return
    interval = max(1.0, settings.tool_audit_cleanup_interval_sec)
    _cleanup_task = asyncio.create_task(_tool_audit_cleanup_loop(interval))
    logger.info("工具审计清理已启动，间隔 %.1f 秒", interval)


async def _tool_audit_cleanup_loop(interval: float) -> None:
    """周期清理；单轮失败不影响后续轮次。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await cleanup_expired_tool_audits()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("工具审计清理巡检失败（下一周期继续）")


async def stop_tool_audit_cleanup() -> None:
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
