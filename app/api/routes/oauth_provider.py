"""管理员维护 OIDC 登录供应商。"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminDep, DbSession
from app.core.errors import ConflictError, NotFoundError
from app.models.oauth import OAuthProvider
from app.repo.oauth_provider_repo import oauth_provider_repo
from app.schemas.oauth_provider import (
    OAuthProviderAdminOut,
    OAuthProviderCreate,
    OAuthProviderUpdate,
)

router = APIRouter(prefix="/api/oauth-providers", tags=["oauth-providers"])


def _out(row: OAuthProvider) -> OAuthProviderAdminOut:
    """把 ORM 行转成不含 secret 的管理员响应。"""
    return OAuthProviderAdminOut(
        code=row.code,
        display_name=row.display_name,
        issuer=row.issuer,
        discovery_url=row.discovery_url,
        authorization_endpoint=row.authorization_endpoint,
        token_endpoint=row.token_endpoint,
        jwks_uri=row.jwks_uri,
        client_id=row.client_id,
        has_client_secret=bool(row.client_secret_cipher),
        token_auth_method=row.token_auth_method,
        scopes=row.scopes.split(),
        username_claim=row.username_claim,
        auto_create=bool(row.auto_create),
        enabled=bool(row.enabled),
    )


@router.get("", response_model=list[OAuthProviderAdminOut])
async def list_providers(
    db: DbSession, admin: AdminDep
) -> list[OAuthProviderAdminOut]:
    """列出当前管理员租户的登录供应商。"""
    rows = await oauth_provider_repo.list(db, admin.tenant_id)
    return [_out(row) for row in rows]


@router.post("", response_model=OAuthProviderAdminOut, status_code=201)
async def create_provider(
    payload: OAuthProviderCreate, db: DbSession, admin: AdminDep
) -> OAuthProviderAdminOut:
    """新增登录供应商；client secret 加密后落库。"""
    if await oauth_provider_repo.get(db, payload.code, admin.tenant_id):
        raise ConflictError("供应商 code 已存在")
    fields = payload.model_dump(exclude={"client_secret", "scopes"})
    fields["scopes"] = " ".join(payload.scopes)
    fields["auto_create"] = int(payload.auto_create)
    fields["enabled"] = int(payload.enabled)
    row = await oauth_provider_repo.create(
        db,
        tenant_id=admin.tenant_id,
        client_secret=payload.client_secret,
        **fields,
    )
    return _out(row)


@router.get("/{code}", response_model=OAuthProviderAdminOut)
async def get_provider(
    code: str, db: DbSession, admin: AdminDep
) -> OAuthProviderAdminOut:
    """读取单个登录供应商。"""
    row = await oauth_provider_repo.get(db, code, admin.tenant_id)
    if row is None:
        raise NotFoundError("供应商不存在")
    return _out(row)


@router.put("/{code}", response_model=OAuthProviderAdminOut)
async def update_provider(
    code: str,
    payload: OAuthProviderUpdate,
    db: DbSession,
    admin: AdminDep,
) -> OAuthProviderAdminOut:
    """局部更新供应商；未传 secret 时保持已加密原值。"""
    row = await oauth_provider_repo.get(db, code, admin.tenant_id)
    if row is None:
        raise NotFoundError("供应商不存在")
    changed = payload.model_dump(exclude_unset=True)
    secret_marker: str | None | object = ...
    if "client_secret" in changed:
        secret_marker = changed.pop("client_secret")
    if "scopes" in changed:
        changed["scopes"] = " ".join(changed["scopes"])
    for key in ("auto_create", "enabled"):
        if key in changed and changed[key] is not None:
            changed[key] = int(changed[key])
    row = await oauth_provider_repo.update(
        db,
        row,
        client_secret=secret_marker,
        **changed,
    )
    return _out(row)


@router.delete("/{code}", status_code=204)
async def delete_provider(code: str, db: DbSession, admin: AdminDep) -> None:
    """删除尚未被任何身份绑定的供应商配置。"""
    row = await oauth_provider_repo.get(db, code, admin.tenant_id)
    if row is None:
        raise NotFoundError("供应商不存在")
    try:
        await oauth_provider_repo.delete(db, row)
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("供应商已有用户身份绑定，不能删除") from exc
