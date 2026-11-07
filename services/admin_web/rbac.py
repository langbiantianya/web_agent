"""Casbin 策略加载、角色同步与权限判断。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import sqlalchemy as sa
from casbin import Enforcer, Model
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_control import AuthzPolicyVersion, CasbinRule

CASBIN_MODEL = """
[request_definition]
r = sub, dom, obj, act

[policy_definition]
p = sub, dom, obj, act

[role_definition]
g = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub, r.dom) && (p.dom == "*" || keyMatch2(r.dom, p.dom)) && (p.obj == "*" || keyMatch2(r.obj, p.obj)) && (p.act == "*" || r.act == p.act)
"""

DEFAULT_POLICIES: tuple[tuple[str, ...], ...] = (
    # OAuth 后台角色的最小权限。
    ("role:viewer", "tenant:*", "admin", "read"),
    ("role:viewer", "tenant:*", "workflow", "read"),
    ("role:viewer", "tenant:*", "agent", "read"),
    ("role:viewer", "tenant:*", "skill", "read"),
    ("role:viewer", "tenant:*", "mcp", "read"),
    ("role:viewer", "tenant:*", "llm", "read"),
    ("role:viewer", "tenant:*", "audit", "read"),
    ("role:operator", "tenant:*", "workflow", "write"),
    ("role:operator", "tenant:*", "agent", "write"),
    ("role:operator", "tenant:*", "skill", "write"),
    ("role:operator", "tenant:*", "mcp", "refresh"),
    ("role:tenant_admin", "tenant:*", "mcp", "write"),
    ("role:tenant_admin", "tenant:*", "llm", "write"),
    ("role:tenant_admin", "tenant:*", "admin_user", "read"),
    ("role:tenant_admin", "tenant:*", "admin_ui", "read"),
    ("role:tenant_admin", "tenant:*", "admin_ui", "manage"),
    ("role:tenant_admin", "tenant:*", "casbin", "mapping_manage"),
)

ROLE_IMPLICATION: dict[str, set[str]] = {
    "operator": {"viewer"},
    "tenant_admin": {"operator", "viewer"},
}


@dataclass(frozen=True)
class RoleMappingRow:
    """Casbin g 规则的可读投影。"""

    role: str
    domain: str


class CasbinService:
    """进程内内存 Enforcer + 数据库版本刷新。"""

    def __init__(self) -> None:
        """构造不连接同步数据库的 Casbin 模型。"""
        model = Model()
        model.load_model_from_text(CASBIN_MODEL)
        self._enforcer = Enforcer(model)
        self._version = -1
        self._lock = asyncio.Lock()

    async def _db_version(self, db: AsyncSession) -> int:
        """读取全局 Casbin 策略版本。"""
        value = await db.scalar(
            sa.select(AuthzPolicyVersion.version).where(AuthzPolicyVersion.id == 1)
        )
        return int(value or 1)

    async def refresh(self, db: AsyncSession, *, force: bool = False) -> None:
        """从数据库刷新进程内 p/g 规则。"""
        version = await self._db_version(db)
        if not force and version == self._version:
            return
        async with self._lock:
            version = await self._db_version(db)
            if not force and version == self._version:
                return
            rows = list(
                await db.scalars(
                    sa.select(CasbinRule).order_by(CasbinRule.id.asc())
                )
            )
            self._enforcer.clear_policy()
            for row in rows:
                values = [
                    value
                    for value in (row.v0, row.v1, row.v2, row.v3, row.v4, row.v5)
                    if value is not None
                ]
                if row.ptype == "p":
                    self._enforcer.add_policy(*values)
                elif row.ptype == "g":
                    self._enforcer.add_grouping_policy(*values)
            self._version = version

    async def can(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        tenant_id: str,
        object_name: str,
        action: str,
    ) -> bool:
        """检查租户或全局域是否允许某个 OAuth 管理员操作。"""
        await self.refresh(db)
        subject = f"user:{user_id}"
        local_domain = f"tenant:{tenant_id}"
        if self._enforcer.enforce(subject, local_domain, object_name, action):
            return True
        return bool(
            self._enforcer.enforce(subject, "*", object_name, action)
        )

    async def replace_user_roles(
        self,
        db: AsyncSession,
        *,
        user_id: str,
        tenant_id: str,
        role_codes: list[str],
    ) -> None:
        """原子替换用户在单个租户域的 Casbin g 规则。"""
        subject = f"user:{user_id}"
        domain = f"tenant:{tenant_id}"
        expanded_roles = set(role_codes)
        for role in tuple(expanded_roles):
            expanded_roles.update(ROLE_IMPLICATION.get(role, set()))
        await db.execute(
            sa.delete(CasbinRule).where(
                CasbinRule.ptype == "g",
                CasbinRule.v0 == subject,
                CasbinRule.v2 == domain,
            )
        )
        for role in sorted(expanded_roles):
            db.add(
                CasbinRule(
                    ptype="g",
                    v0=subject,
                    v1=f"role:{role}",
                    v2=domain,
                )
            )
        await self.bump_version(db)
        await db.flush()

    async def bump_version(self, db: AsyncSession) -> int:
        """递增策略版本并返回新版本。"""
        await db.execute(
            sa.update(AuthzPolicyVersion)
            .where(AuthzPolicyVersion.id == 1)
            .values(version=AuthzPolicyVersion.version + 1)
        )
        version = await self._db_version(db)
        await db.flush()
        return version

    async def role_rows(
        self, db: AsyncSession, *, user_id: str, tenant_id: str
    ) -> list[RoleMappingRow]:
        """列出用户在某租户的 Casbin role 与 domain。"""
        subject = f"user:{user_id}"
        rows = await db.scalars(
            sa.select(CasbinRule).where(
                CasbinRule.ptype == "g",
                CasbinRule.v0 == subject,
                sa.or_(
                    CasbinRule.v2 == f"tenant:{tenant_id}",
                    CasbinRule.v2 == "*",
                ),
            )
        )
        return [
            RoleMappingRow(role=(row.v1 or "").removeprefix("role:"), domain=row.v2 or "")
            for row in rows
        ]

    async def role_permissions(
        self, db: AsyncSession, *, role_code: str, tenant_id: str
    ) -> list[tuple[str, str]]:
        """列出某角色在租户或全局域的权限。"""
        rows = await db.scalars(
            sa.select(CasbinRule).where(
                CasbinRule.ptype == "p",
                CasbinRule.v0 == f"role:{role_code}",
                sa.or_(
                    CasbinRule.v1 == f"tenant:{tenant_id}",
                    CasbinRule.v1 == "tenant:*",
                    CasbinRule.v1 == "*",
                ),
            )
        )
        return [(row.v2 or "", row.v3 or "") for row in rows]

    async def list_policies(
        self,
        db: AsyncSession,
        *,
        tenant_id: str | None = None,
    ) -> list[CasbinRule]:
        """返回全部或指定租户可见的 Casbin 规则。"""
        statement = sa.select(CasbinRule)
        if tenant_id:
            domain = f"tenant:{tenant_id}"
            statement = statement.where(
                sa.or_(
                    sa.and_(
                        CasbinRule.ptype == "p",
                        CasbinRule.v1.in_((domain, "tenant:*", "*")),
                    ),
                    sa.and_(
                        CasbinRule.ptype == "g",
                        CasbinRule.v2.in_((domain, "*")),
                    ),
                )
            )
        return list(
            await db.scalars(
                statement.order_by(CasbinRule.ptype, CasbinRule.id)
            )
        )

    async def seed_defaults(self, db: AsyncSession) -> None:
        """初始化版本行和内置角色 policy，重复执行安全。"""
        if await db.scalar(sa.select(AuthzPolicyVersion.id)) is None:
            db.add(AuthzPolicyVersion(id=1, version=1))
            await db.flush()
        existing = await db.scalar(sa.select(sa.func.count()).select_from(CasbinRule))
        if existing:
            return
        for index, policy in enumerate(DEFAULT_POLICIES, start=1):
            values = list(policy)
            db.add(
                CasbinRule(
                    ptype="p",
                    v0=values[0],
                    v1=values[1],
                    v2=values[2],
                    v3=values[3],
                )
            )
        await db.commit()
        await self.refresh(db, force=True)


casbin_service = CasbinService()
