"""管理员用户数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin import admin_key_hash, new_admin_key
from app.models.admin_user import AdminUser


class AdminUserRepo:
    """管理员用户的 CRUD 与 Key 轮换。"""

    async def get(self, db: AsyncSession, username: str) -> AdminUser | None:
        """按用户名取用户。"""
        return await db.scalar(
            sa.select(AdminUser).where(AdminUser.username == username)
        )

    async def list(
        self, db: AsyncSession, *, tenant_id: str | None, limit: int, offset: int
    ) -> list[AdminUser]:
        """按租户列出用户。"""
        stmt = sa.select(AdminUser)
        if tenant_id:
            stmt = stmt.where(AdminUser.tenant_id == tenant_id)
        stmt = stmt.order_by(AdminUser.id.desc()).limit(limit).offset(offset)
        return list(await db.scalars(stmt))

    async def create(
        self,
        db: AsyncSession,
        *,
        username: str,
        display_name: str,
        tenant_id: str,
        role: str,
        enabled: bool,
    ) -> tuple[AdminUser, str]:
        """创建用户，返回记录与一次性明文 Key。"""
        api_key = new_admin_key()
        row = AdminUser(
            username=username,
            display_name=display_name,
            tenant_id=tenant_id,
            role=role,
            api_key_hash=admin_key_hash(api_key),
            api_key_hint=api_key[-6:],
            enabled=1 if enabled else 0,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row, api_key

    async def update(
        self, db: AsyncSession, username: str, fields: dict
    ) -> AdminUser | None:
        """局部更新用户并递增版本。"""
        row = await self.get(db, username)
        if row is None:
            return None
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        await db.execute(
            sa.update(AdminUser)
            .where(AdminUser.username == username)
            .values(version=AdminUser.version + 1)
        )
        await db.commit()
        await db.refresh(row)
        return row

    async def rotate_key(
        self, db: AsyncSession, username: str
    ) -> tuple[AdminUser, str] | None:
        """轮换 Key；旧 Key 立即失效。"""
        row = await self.get(db, username)
        if row is None:
            return None
        api_key = new_admin_key()
        row.api_key_hash = admin_key_hash(api_key)
        row.api_key_hint = api_key[-6:]
        row.version = (row.version or 1) + 1
        await db.commit()
        await db.refresh(row)
        return row, api_key

    async def delete(self, db: AsyncSession, username: str) -> bool:
        """删除数据库管理员用户。"""
        row = await self.get(db, username)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
        return True


admin_user_repo = AdminUserRepo()
