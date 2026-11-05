"""数据库管理员用户管理。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession, SuperAdminDep
from app.core.errors import ConflictError, NotFoundError
from app.repo.admin_user_repo import admin_user_repo
from app.schemas.admin_user import (
    AdminKeyRotateOut,
    AdminUserCreate,
    AdminUserCreatedOut,
    AdminUserOut,
    AdminUserUpdate,
)

router = APIRouter(prefix="/api/admin-users", tags=["admin-users"])


def _to_out(row) -> AdminUserOut:
    """ORM 转响应，不包含 Key 摘要。"""
    return AdminUserOut(
        id=row.id,
        username=row.username,
        display_name=row.display_name or "",
        tenant_id=row.tenant_id,
        role=row.role,
        api_key_hint=row.api_key_hint or "",
        enabled=bool(row.enabled),
        version=row.version or 1,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[AdminUserOut])
async def list_admin_users(
    db: DbSession,
    _admin: SuperAdminDep,
    tenant_id: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[AdminUserOut]:
    """列出数据库管理员用户。"""
    rows = await admin_user_repo.list(
        db, tenant_id=tenant_id, limit=limit, offset=offset
    )
    return [_to_out(row) for row in rows]


@router.post("", response_model=AdminUserCreatedOut, status_code=201)
async def create_admin_user(
    payload: AdminUserCreate,
    db: DbSession,
    _admin: SuperAdminDep,
) -> AdminUserCreatedOut:
    """创建管理员；明文 API Key 只在本次响应中出现。"""
    if await admin_user_repo.get(db, payload.username):
        raise ConflictError(f"管理员用户名已存在：{payload.username}")
    row, api_key = await admin_user_repo.create(
        db,
        username=payload.username,
        display_name=payload.display_name,
        tenant_id=payload.tenant_id,
        role=payload.role,
        enabled=payload.enabled,
    )
    return AdminUserCreatedOut(**_to_out(row).model_dump(), api_key=api_key)


@router.get("/{username}", response_model=AdminUserOut)
async def get_admin_user(
    username: str,
    db: DbSession,
    _admin: SuperAdminDep,
) -> AdminUserOut:
    """查询单个管理员。"""
    row = await admin_user_repo.get(db, username)
    if row is None:
        raise NotFoundError(f"管理员不存在：{username}")
    return _to_out(row)


@router.put("/{username}", response_model=AdminUserOut)
async def update_admin_user(
    username: str,
    payload: AdminUserUpdate,
    db: DbSession,
    _admin: SuperAdminDep,
) -> AdminUserOut:
    """更新角色或启停状态。"""
    fields = payload.model_dump(exclude_unset=True)
    if username == _admin.username and not _admin.bootstrap:
        if fields.get("enabled") is False:
            raise ConflictError("不能停用当前登录管理员自己")
        if fields.get("role") not in (None, "admin"):
            raise ConflictError("不能降低当前登录管理员的角色")
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    row = await admin_user_repo.update(db, username, fields)
    if row is None:
        raise NotFoundError(f"管理员不存在：{username}")
    return _to_out(row)


@router.post("/{username}/rotate-key", response_model=AdminKeyRotateOut)
async def rotate_admin_key(
    username: str,
    db: DbSession,
    _admin: SuperAdminDep,
) -> AdminKeyRotateOut:
    """轮换 Key；旧 Key 立即失效。"""
    result = await admin_user_repo.rotate_key(db, username)
    if result is None:
        raise NotFoundError(f"管理员不存在：{username}")
    row, api_key = result
    return AdminKeyRotateOut(
        username=row.username,
        api_key=api_key,
        version=row.version,
    )


@router.delete("/{username}", status_code=204)
async def delete_admin_user(
    username: str,
    db: DbSession,
    admin: SuperAdminDep,
) -> None:
    """删除管理员，禁止删除当前登录主体自己。"""
    if username == admin.username and not admin.bootstrap:
        raise ConflictError("不能删除当前登录管理员自己")
    if not await admin_user_repo.delete(db, username):
        raise NotFoundError(f"管理员不存在：{username}")
