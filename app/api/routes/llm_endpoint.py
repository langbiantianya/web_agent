"""LLM 接口配置管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import AdminDep, DbSession
from app.core.crypto import encrypt
from app.core.errors import ConflictError, NotFoundError
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.schemas.llm_endpoint import (
    LlmEndpointCreate,
    LlmEndpointOut,
    LlmEndpointUpdate,
)

router = APIRouter(prefix="/api/llm-endpoints", tags=["llm-endpoints"])


def _to_out(row) -> LlmEndpointOut:
    """ORM 行转响应模型，密钥只暴露 ``has_api_key``。"""
    return LlmEndpointOut(
        id=row.id,
        code=row.code,
        name=row.name or row.code,
        base_url=row.base_url,
        priority=row.priority,
        enabled=bool(row.enabled),
        version=row.version or 1,
        has_api_key=bool(row.api_key_cipher),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[LlmEndpointOut])
async def list_endpoints(
    db: DbSession,
    _admin: AdminDep,
    only_enabled: bool = Query(False),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[LlmEndpointOut]:
    """按优先级列出接口配置。"""
    rows = await llm_endpoint_repo.list(
        db, only_enabled=only_enabled, limit=limit, offset=offset
    )
    return [_to_out(row) for row in rows]


@router.post("", response_model=LlmEndpointOut, status_code=201)
async def create_endpoint(
    payload: LlmEndpointCreate,
    db: DbSession,
    _admin: AdminDep,
) -> LlmEndpointOut:
    """新增接口配置。code 是稳定标识，密钥加密后落库。"""
    if await llm_endpoint_repo.get_by_code(db, payload.code):
        raise ConflictError(f"LLM 接口 code 已存在：{payload.code}")
    row = await llm_endpoint_repo.create(
        db,
        code=payload.code,
        name=payload.name or payload.code,
        base_url=payload.base_url,
        api_key_cipher=encrypt(payload.api_key),
        priority=payload.priority,
        enabled=1 if payload.enabled else 0,
    )
    return _to_out(row)


@router.get("/{code}", response_model=LlmEndpointOut)
async def get_endpoint(
    code: str,
    db: DbSession,
    _admin: AdminDep,
) -> LlmEndpointOut:
    """查询单个接口配置。"""
    row = await llm_endpoint_repo.get_by_code(db, code)
    if row is None:
        raise NotFoundError(f"LLM 接口不存在：{code}")
    return _to_out(row)


@router.put("/{code}", response_model=LlmEndpointOut)
async def update_endpoint(
    code: str,
    payload: LlmEndpointUpdate,
    db: DbSession,
    _admin: AdminDep,
) -> LlmEndpointOut:
    """更新接口配置；省略 api_key 保持密钥，显式传空串则清除。"""
    fields = payload.model_dump(exclude_unset=True)
    if "api_key" in fields:
        fields["api_key_cipher"] = encrypt(fields.pop("api_key"))
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    row = await llm_endpoint_repo.update(db, code, fields)
    if row is None:
        raise NotFoundError(f"LLM 接口不存在：{code}")
    return _to_out(row)


@router.delete("/{code}", status_code=204)
async def delete_endpoint(code: str, db: DbSession, _admin: AdminDep) -> None:
    """删除接口配置。"""
    if not await llm_endpoint_repo.delete(db, code):
        raise NotFoundError(f"LLM 接口不存在：{code}")
