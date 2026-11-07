"""OAuthProvider 数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt
from app.models.oauth import OAuthProvider


class OAuthProviderRepo:
    async def get(
        self, db: AsyncSession, code: str, tenant_id: str
    ) -> OAuthProvider | None:
        """按租户与公开 code 查询供应商。"""
        return await db.scalar(
            sa.select(OAuthProvider).where(
                OAuthProvider.tenant_id == tenant_id,
                OAuthProvider.code == code,
            )
        )

    async def get_by_id(self, db: AsyncSession, provider_id: int) -> OAuthProvider | None:
        """按内部主键查询供应商。"""
        return await db.get(OAuthProvider, provider_id)

    async def list(
        self, db: AsyncSession, tenant_id: str, enabled_only: bool = False
    ) -> list[OAuthProvider]:
        """列出供应商，按 code 稳定排序。"""
        stmt = sa.select(OAuthProvider).where(OAuthProvider.tenant_id == tenant_id)
        if enabled_only:
            stmt = stmt.where(OAuthProvider.enabled == 1)
        return list(await db.scalars(stmt.order_by(OAuthProvider.code.asc())))

    async def create(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        client_secret: str | None,
        **fields,
    ) -> OAuthProvider:
        """创建供应商；client secret 只以密文落库。"""
        row = OAuthProvider(
            tenant_id=tenant_id,
            client_secret_cipher=encrypt(client_secret),
            **fields,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    async def update(
        self,
        db: AsyncSession,
        row: OAuthProvider,
        *,
        client_secret: str | None | object = ...,
        **fields,
    ) -> OAuthProvider:
        """局部更新供应商；省略 secret 表示保持原值，空串表示清除。"""
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        if client_secret is not ...:
            row.client_secret_cipher = (
                encrypt(client_secret)
                if isinstance(client_secret, str) and client_secret
                else None
            )
        await db.commit()
        await db.refresh(row)
        return row

    async def delete(self, db: AsyncSession, row: OAuthProvider) -> None:
        """删除供应商配置。"""
        await db.delete(row)
        await db.commit()


oauth_provider_repo = OAuthProviderRepo()
