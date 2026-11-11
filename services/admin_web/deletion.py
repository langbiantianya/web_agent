"""管理端「删除可撤销」的服务层。

删除资源是硬删除：行没了，引用它的编排随即失效，配置也只能凭记忆重填。
这里给删除操作加一条**短时撤销**通道：

1. 删除前把整行快照写进 :class:`AdminDeletedResource`；
2. 删除成功后跳回列表页，附带一个一次性令牌；
3. 管理员在撤销窗口内点「撤销」，服务端按快照把行原样重建。

## 为什么是「短时」而不是永久软删除

永久软删除要求每一处读路径都记得过滤 ``deleted_at``——漏一处就等于
「已删除的资源还在生效」，比硬删除更危险。短时快照把风险面收在**一条
写入路径 + 一条撤销路径**上：不做删除的资源过滤，快照过期后自动失效。

## 快照的边界

- 快照只保存**重建该行所需**的字段，各资源类型的字段清单由
  :data:`SNAPSHOT_FIELDS` 显式声明（不反射整行，避免把无关列带进去）；
- 快照表只由发起删除的管理员本人撤销（``actor_id`` 比对）；
- 密文列按原样保存——它本来就是密文，与落在原表里风险相同；
- 撤销窗口默认 30 分钟（:data:`UNDO_WINDOW_SECONDS`）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_deleted_resource import AdminDeletedResource
from app.models.base import utcnow

# 撤销窗口：30 分钟。足够管理员发现误删并点回来，又不至于让删除
# 长期处于「随时可能被撤销」的状态。
UNDO_WINDOW_SECONDS = 30 * 60

# 各资源类型参与快照的字段。**显式声明**而不是反射整行：反射会把
# created_at / updated_at 之类的托管列也带进去，重建时要么被服务端
# 覆盖、要么写出与原表不一致的值。显式清单也让「快照里有什么」可审。
SNAPSHOT_FIELDS: dict[str, tuple[str, ...]] = {
    "agent": (
        "code",
        "tenant_id",
        "name",
        "description",
        "system_prompt",
        "llm_endpoint_code",
        "model",
        "base_url",
        "api_key_cipher",
        "temperature",
        "max_tokens",
        "tools",
        "tool_policy",
        "skills",
        "extra_params",
        "enabled",
    ),
    "llm_endpoint": (
        "code",
        "name",
        "base_url",
        "model",
        "api_key_cipher",
        "priority",
        "enabled",
    ),
    "mcp_server": (
        "code",
        "tenant_id",
        "name",
        "transport",
        "command",
        "args",
        "env",
        "headers",
        "url",
        "auth_type",
        "oauth_token_url",
        "oauth_revocation_url",
        "oauth_client_id",
        "oauth_client_secret_cipher",
        "oauth_scopes",
        "oauth_resource",
        "allow_tools",
        "timeout_sec",
        "enabled",
    ),
    "workflow": (
        "code",
        "name",
        "description",
        "definition",
        "enabled",
    ),
    # 平台超级管理员没有 ``code`` 列，它的主键是用户名。快照里仍然把它
    # 放在 ``code`` 位上（见 :data:`SNAPSHOT_KEYS`），这样撤销逻辑可以
    # 对所有资源类型用同一套「按 code 查重」的判断。
    "super_admin_account": (
        "username",
        "password_hash",
        "email",
        "phone",
        "status",
        "owner_slot",
        "password_rotated_at",
        "password_expires_at",
        "password_version",
        "failed_login_count",
        "locked_until",
        "created_by_id",
        "version",
    ),
}

# 资源类型 → 主键字段名。绝大多数资源用 ``code``；平台超级管理员用
# ``username``。快照统一把主键写进 ``code`` 位，重建逻辑因此只需要
# 一种「按 code 查重」的写法。
SNAPSHOT_KEYS: dict[str, str] = {
    "super_admin_account": "username",
}


class UndoNotAvailable(Exception):
    """撤销请求无法执行（快照不存在、已过期或不属于当前管理员）。"""


def json_safe(value: Any) -> Any:
    """把一个取值转成 **JSON 列能存**的形式。

    数据库里有些列不是 JSON 原生类型：``temperature`` 是 ``Decimal``、
    ``extra_params`` 里可能藏着 ``Decimal``。直接塞进 JSON 列会在
    ``json.dumps`` 时抛 ``TypeError``——那会让「删除」直接 500，删除
    根本做不成。因此这里统一转换：``Decimal`` 转 ``float``（数值语义
    不变），``datetime`` 转 ISO 字符串，其余未知类型退化成字符串。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)


def _as_datetime(value: Any) -> datetime | None:
    """把快照里的 ISO 字符串还原成 ``datetime``。

    快照走 JSON 列，时间列自然只能存字符串；重建时必须还原成
    ``datetime``，否则驱动会把字符串直接塞进 DATETIME 列而报错。
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def snapshot_values(
    resource_type: str,
    row: Any,
) -> dict[str, Any]:
    """按 :data:`SNAPSHOT_FIELDS` 抽取一行的可重建字段。"""
    fields = SNAPSHOT_FIELDS.get(resource_type)
    if fields is None:
        raise UndoNotAvailable(f"unsupported resource type: {resource_type}")
    values = {
        name: json_safe(getattr(row, name, None))
        for name in fields
    }
    # 主键统一落在 ``code`` 位上，重建时按它查重。
    key_field = SNAPSHOT_KEYS.get(resource_type, "code")
    values["code"] = values.get(key_field)
    return values


async def record_deletion(
    db: AsyncSession,
    *,
    resource_type: str,
    row: Any,
    actor_id: int | None,
    actor_username: str,
) -> AdminDeletedResource:
    """在删除前登记一条快照，返回可供撤销的记录。"""
    key_field = SNAPSHOT_KEYS.get(resource_type, "code")
    entry = AdminDeletedResource(
        resource_type=resource_type,
        resource_code=str(getattr(row, key_field, "") or ""),
        actor_id=actor_id,
        actor_username=actor_username[:64],
        payload=snapshot_values(resource_type, row),
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(seconds=UNDO_WINDOW_SECONDS),
    )
    db.add(entry)
    await db.flush()
    return entry


async def undo_deletion(
    db: AsyncSession,
    *,
    entry_id: int,
    actor_id: int | None,
    actor_username: str,
) -> tuple[str, str]:
    """按快照重建被删除的资源，返回 ``(资源类型, 资源编码)``。

    Raises:
        UndoNotAvailable: 快照不存在、已过期、不是当前管理员删的，
            或目标编码已被占用（重建会撞唯一键）。
    """
    entry = await db.get(AdminDeletedResource, entry_id)
    if entry is None:
        raise UndoNotAvailable("snapshot_missing")
    if entry.actor_id != actor_id:
        raise UndoNotAvailable("snapshot_not_owner")
    now = utcnow()
    if entry.expires_at is not None and entry.expires_at <= now:
        await db.delete(entry)
        await db.commit()
        raise UndoNotAvailable("snapshot_expired")
    payload = dict(entry.payload or {})
    code = str(payload.get("code") or "")
    if not code:
        raise UndoNotAvailable("snapshot_invalid")
    await _rebuild(db, entry.resource_type, payload)
    await db.delete(entry)
    await db.commit()
    return entry.resource_type, code


async def _rebuild(
    db: AsyncSession,
    resource_type: str,
    payload: dict[str, Any],
) -> None:
    """按快照重建一行。重建失败时向上抛错，由调用方转成提示。"""
    if resource_type == "agent":
        await _rebuild_agent(db, payload)
    elif resource_type == "llm_endpoint":
        await _rebuild_llm_endpoint(db, payload)
    elif resource_type == "mcp_server":
        await _rebuild_mcp_server(db, payload)
    elif resource_type == "workflow":
        await _rebuild_workflow(db, payload)
    elif resource_type == "super_admin_account":
        await _rebuild_super_admin_account(db, payload)
    else:  # pragma: no cover - SNAPSHOT_FIELDS 已经限制了取值
        raise UndoNotAvailable(f"unsupported resource type: {resource_type}")


async def _rebuild_agent(db: AsyncSession, payload: dict[str, Any]) -> None:
    """重建 Agent 行。"""
    from app.models.agent import Agent

    if await _agent_exists(db, str(payload.get("code") or "")):
        raise UndoNotAvailable("code_taken")
    db.add(Agent(**payload))


async def _agent_exists(db: AsyncSession, code: str) -> bool:
    """判断 Agent 编码是否已被占用。"""
    from app.models.agent import Agent

    return (
        await db.scalar(
            sa.select(Agent.id).where(Agent.code == code).limit(1)
        )
    ) is not None


async def _rebuild_llm_endpoint(
    db: AsyncSession, payload: dict[str, Any]
) -> None:
    """重建 LLM 端点行。"""
    from app.models.llm_endpoint import LlmEndpoint

    if await db.scalar(
        sa.select(LlmEndpoint.id)
        .where(LlmEndpoint.code == str(payload.get("code") or ""))
        .limit(1)
    ) is not None:
        raise UndoNotAvailable("code_taken")
    db.add(LlmEndpoint(**payload))


async def _rebuild_mcp_server(
    db: AsyncSession, payload: dict[str, Any]
) -> None:
    """重建 MCP 服务器行。工具快照不在撤销范围内——它由刷新重建。"""
    from app.models.mcp_server import McpServer

    if await db.scalar(
        sa.select(McpServer.id)
        .where(McpServer.code == str(payload.get("code") or ""))
        .limit(1)
    ) is not None:
        raise UndoNotAvailable("code_taken")
    db.add(McpServer(**payload))


async def _rebuild_super_admin_account(
    db: AsyncSession, payload: dict[str, Any]
) -> None:
    """重建平台超级管理员账号。

    密码只存 Argon2id 摘要，快照里带的就是那串摘要——恢复后管理员仍用
    原口令登录，不需要重置。
    """
    from app.models.admin_control import SuperAdminAccount

    username = str(payload.get("code") or payload.get("username") or "")
    if not username:
        raise UndoNotAvailable("snapshot_invalid")
    if await db.scalar(
        sa.select(SuperAdminAccount.id)
        .where(SuperAdminAccount.username == username)
        .limit(1)
    ) is not None:
        raise UndoNotAvailable("code_taken")
    db.add(
        SuperAdminAccount(
            username=username,
            password_hash=str(payload.get("password_hash") or ""),
            email=payload.get("email"),
            phone=payload.get("phone"),
            status=str(payload.get("status") or "active"),
            owner_slot=payload.get("owner_slot"),
            password_rotated_at=_as_datetime(payload.get("password_rotated_at")),
            password_expires_at=_as_datetime(payload.get("password_expires_at")),
            password_version=int(payload.get("password_version") or 1),
            failed_login_count=int(payload.get("failed_login_count") or 0),
            locked_until=_as_datetime(payload.get("locked_until")),
            created_by_id=payload.get("created_by_id"),
            version=int(payload.get("version") or 1),
        )
    )


async def _rebuild_workflow(
    db: AsyncSession, payload: dict[str, Any]
) -> None:
    """重建 Workflow 行。"""
    from app.models.workflow import Workflow

    if await db.scalar(
        sa.select(Workflow.id)
        .where(Workflow.code == str(payload.get("code") or ""))
        .limit(1)
    ) is not None:
        raise UndoNotAvailable("code_taken")
    db.add(Workflow(**payload))


async def purge_expired(
    db: AsyncSession, *, now: Any | None = None
) -> int:
    """清掉过期快照，返回清理条数。"""
    moment = now or utcnow()
    result = await db.execute(
        sa.delete(AdminDeletedResource).where(
            AdminDeletedResource.expires_at <= moment
        )
    )
    await db.commit()
    return int(result.rowcount or 0)


__all__ = [
    "SNAPSHOT_FIELDS",
    "SNAPSHOT_KEYS",
    "json_safe",
    "UNDO_WINDOW_SECONDS",
    "UndoNotAvailable",
    "purge_expired",
    "record_deletion",
    "snapshot_values",
    "undo_deletion",
]

