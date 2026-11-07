"""Ludic 页面/组件注册表与数据库 UI 权限绑定。"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_control import (
    AdminComponentDefinition,
    AdminComponentPermission,
    AdminPageDefinition,
    AdminPagePermission,
    permission_binding_scope,
)
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.rbac import casbin_service


@dataclass(frozen=True)
class ComponentSpec:
    """一个代码注册的后台组件。"""

    code: str
    render_key: str
    label: str
    kind: str
    permissions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PageSpec:
    """一个代码注册的后台页面。"""

    code: str
    route: str
    title: str
    nav_group: str
    nav_order: int
    permissions: tuple[tuple[str, str], ...]
    components: tuple[ComponentSpec, ...] = field(default_factory=tuple)


PAGE_SPECS: tuple[PageSpec, ...] = (
    PageSpec(
        code="dashboard",
        route="/admin/",
        title="Overview",
        nav_group="Overview",
        nav_order=10,
        permissions=(("admin", "read"),),
        components=(
            ComponentSpec(
                "metrics",
                "dashboard.metrics",
                "Platform metrics",
                "metric",
                (("admin", "read"),),
            ),
            ComponentSpec(
                "quick_actions",
                "dashboard.quick_actions",
                "Quick actions",
                "action",
                (("admin_ui", "manage"),),
            ),
        ),
    ),
    PageSpec(
        code="workflows",
        route="/admin/workflows",
        title="Workflows",
        nav_group="Resources",
        nav_order=100,
        permissions=(("workflow", "read"),),
        components=(
            ComponentSpec(
                "workflow_table",
                "workflows.table",
                "Workflow list",
                "data_table",
                (("workflow", "read"),),
            ),
            ComponentSpec(
                "workflow_editor",
                "workflows.editor",
                "Workflow editor",
                "editor",
                (("workflow", "write"),),
            ),
        ),
    ),
    PageSpec(
        code="agents",
        route="/admin/agents",
        title="Agents",
        nav_group="Resources",
        nav_order=110,
        permissions=(("agent", "read"),),
        components=(
            ComponentSpec(
                "agent_table",
                "agents.table",
                "Agent list",
                "data_table",
                (("agent", "read"),),
            ),
            ComponentSpec(
                "agent_editor",
                "agents.editor",
                "Agent editor",
                "editor",
                (("agent", "write"),),
            ),
        ),
    ),
    PageSpec(
        code="skills",
        route="/admin/skills",
        title="Skills",
        nav_group="Resources",
        nav_order=120,
        permissions=(("skill", "read"),),
        components=(
            ComponentSpec(
                "skill_table",
                "skills.table",
                "Skill list",
                "data_table",
                (("skill", "read"),),
            ),
            ComponentSpec(
                "skill_editor",
                "skills.editor",
                "Skill editor",
                "editor",
                (("skill", "write"),),
            ),
        ),
    ),
    PageSpec(
        code="mcp",
        route="/admin/mcp",
        title="MCP servers",
        nav_group="Resources",
        nav_order=130,
        permissions=(("mcp", "read"),),
        components=(
            ComponentSpec(
                "mcp_table",
                "mcp.table",
                "MCP server list",
                "data_table",
                (("mcp", "read"),),
            ),
            ComponentSpec(
                "mcp_editor",
                "mcp.editor",
                "MCP server editor",
                "editor",
                (("mcp", "write"),),
            ),
            ComponentSpec(
                "mcp_refresh",
                "mcp.refresh",
                "MCP catalog refresh",
                "action",
                (("mcp", "refresh"),),
            ),
        ),
    ),
    PageSpec(
        code="llm",
        route="/admin/llm",
        title="LLM endpoints",
        nav_group="Resources",
        nav_order=140,
        permissions=(("llm", "read"),),
        components=(
            ComponentSpec(
                "llm_table",
                "llm.table",
                "LLM endpoint list",
                "data_table",
                (("llm", "read"),),
            ),
            ComponentSpec(
                "llm_editor",
                "llm.editor",
                "LLM endpoint editor",
                "editor",
                (("llm", "write"),),
            ),
        ),
    ),
    PageSpec(
        code="audit",
        route="/admin/audit",
        title="Tool audit",
        nav_group="Operations",
        nav_order=150,
        permissions=(("audit", "read"),),
        components=(
            ComponentSpec(
                "audit_table",
                "audit.table",
                "Tool audit list",
                "data_table",
                (("audit", "read"),),
            ),
        ),
    ),
    PageSpec(
        code="access_pages",
        route="/admin/access/pages",
        title="Page permissions",
        nav_group="Access control",
        nav_order=20,
        permissions=(("admin_ui", "manage"),),
        components=(
            ComponentSpec(
                "page_matrix",
                "access.page_matrix",
                "Page permission matrix",
                "editor",
                (("admin_ui", "manage"),),
            ),
        ),
    ),
    PageSpec(
        code="access_components",
        route="/admin/access/components",
        title="Component permissions",
        nav_group="Access control",
        nav_order=30,
        permissions=(("admin_ui", "manage"),),
        components=(
            ComponentSpec(
                "component_matrix",
                "access.component_matrix",
                "Component permission matrix",
                "editor",
                (("admin_ui", "manage"),),
            ),
        ),
    ),
    PageSpec(
        code="access_roles",
        route="/admin/access/roles",
        title="Casbin roles",
        nav_group="Access control",
        nav_order=40,
        permissions=(("casbin", "role_manage"),),
        components=(
            ComponentSpec(
                "policy_list",
                "access.policy_list",
                "Policy rules",
                "data_table",
                (("casbin", "role_manage"),),
            ),
        ),
    ),
    PageSpec(
        code="access_mappings",
        route="/admin/access/mappings",
        title="OAuth role mappings",
        nav_group="Access control",
        nav_order=50,
        permissions=(("casbin", "mapping_manage"),),
        components=(
            ComponentSpec(
                "mapping_list",
                "access.mapping_list",
                "OAuth mappings",
                "data_table",
                (("casbin", "mapping_manage"),),
            ),
        ),
    ),
    PageSpec(
        code="platform_admins",
        route="/admin/platform-admins",
        title="Platform super admins",
        nav_group="Security",
        nav_order=60,
        permissions=(("platform_admin", "owner"),),
        components=(
            ComponentSpec(
                "admin_list",
                "platform.admin_list",
                "Platform admin accounts",
                "data_table",
                (("platform_admin", "owner"),),
            ),
        ),
    ),
    PageSpec(
        code="preferences",
        route="/admin/preferences",
        title="Appearance",
        nav_group="Settings",
        nav_order=70,
        permissions=(("admin", "read"),),
        components=(
            ComponentSpec(
                "theme_form",
                "preferences.theme_form",
                "Theme and appearance",
                "form",
                (("admin", "read"),),
            ),
        ),
    ),
)


def page_spec(code: str) -> PageSpec | None:
    """按 code 查页面注册项。"""
    return next((page for page in PAGE_SPECS if page.code == code), None)


async def seed_registry(db: AsyncSession) -> None:
    """把代码 registry 同步到数据库并补齐默认权限绑定。"""
    await casbin_service.seed_defaults(db)
    for page in PAGE_SPECS:
        row = await db.get(AdminPageDefinition, page.code)
        if row is None:
            db.add(
                AdminPageDefinition(
                    code=page.code,
                    route_key=page.route,
                    title=page.title,
                    nav_group=page.nav_group,
                    nav_order=page.nav_order,
                )
            )
            await db.flush()
        else:
            row.route_key = page.route
            row.title = page.title
            row.nav_group = page.nav_group
            row.nav_order = page.nav_order
        for index, component in enumerate(page.components, start=1):
            comp = await db.get(
                AdminComponentDefinition, (page.code, component.code)
            )
            if comp is None:
                db.add(
                    AdminComponentDefinition(
                        page_code=page.code,
                        code=component.code,
                        render_key=component.render_key,
                        kind=component.kind,
                        label=component.label,
                        sort_order=index * 10,
                    )
                )
            else:
                comp.render_key = component.render_key
                comp.kind = component.kind
                comp.label = component.label
                comp.sort_order = index * 10
    await db.commit()

    for page in PAGE_SPECS:
        definition = await db.get(AdminPageDefinition, page.code)
        if definition is not None and not definition.permissions_initialized:
            exists = await db.scalar(
                sa.select(AdminPagePermission.id).where(
                    AdminPagePermission.page_code == page.code,
                    AdminPagePermission.tenant_id.is_(None),
                )
            )
            if exists is None:
                for object_name, action in page.permissions:
                    db.add(
                        AdminPagePermission(
                            tenant_id=None,
                            binding_scope=permission_binding_scope(None),
                            page_code=page.code,
                            object_name=object_name,
                            action_name=action,
                        )
                    )
            definition.permissions_initialized = 1
        for component in page.components:
            component_row = await db.get(
                AdminComponentDefinition, (page.code, component.code)
            )
            if (
                component_row is None
                or component_row.permissions_initialized
            ):
                continue
            exists_component = await db.scalar(
                sa.select(AdminComponentPermission.id).where(
                    AdminComponentPermission.page_code == page.code,
                    AdminComponentPermission.component_code == component.code,
                    AdminComponentPermission.tenant_id.is_(None),
                )
            )
            if exists_component is None:
                for object_name, action in component.permissions:
                    db.add(
                        AdminComponentPermission(
                            tenant_id=None,
                            binding_scope=permission_binding_scope(None),
                            page_code=page.code,
                            component_code=component.code,
                            object_name=object_name,
                            action_name=action,
                        )
                    )
            component_row.permissions_initialized = 1
    await db.commit()


async def _effective_page_permissions(
    db: AsyncSession, *, page_code: str, tenant_id: str | None
) -> list[tuple[str, str]]:
    """读取 tenant override，缺失时回退全局绑定。"""
    if tenant_id:
        rows = list(
            await db.scalars(
                sa.select(AdminPagePermission).where(
                    AdminPagePermission.page_code == page_code,
                    AdminPagePermission.tenant_id == tenant_id,
                )
            )
        )
        if rows:
            return [(row.object_name, row.action_name) for row in rows]
    rows = list(
        await db.scalars(
            sa.select(AdminPagePermission).where(
                AdminPagePermission.page_code == page_code,
                AdminPagePermission.tenant_id.is_(None),
            )
        )
    )
    return [(row.object_name, row.action_name) for row in rows]


async def _effective_component_permissions(
    db: AsyncSession,
    *,
    page_code: str,
    component_code: str,
    tenant_id: str | None,
) -> list[tuple[str, str]]:
    """读取组件 tenant override，缺失时回退全局。"""
    if tenant_id:
        rows = list(
            await db.scalars(
                sa.select(AdminComponentPermission).where(
                    AdminComponentPermission.page_code == page_code,
                    AdminComponentPermission.component_code == component_code,
                    AdminComponentPermission.tenant_id == tenant_id,
                )
            )
        )
        if rows:
            return [(row.object_name, row.action_name) for row in rows]
    rows = list(
        await db.scalars(
            sa.select(AdminComponentPermission).where(
                AdminComponentPermission.page_code == page_code,
                AdminComponentPermission.component_code == component_code,
                AdminComponentPermission.tenant_id.is_(None),
            )
        )
    )
    return [(row.object_name, row.action_name) for row in rows]


async def _permissions_allow(
    db: AsyncSession,
    principal: AdminPrincipal,
    permissions: list[tuple[str, str]],
    access_mode: str,
) -> bool:
    """按 all/any 规则检查权限；平台超级管理员直接允许。"""
    if principal.is_platform_super_admin:
        return True
    if not permissions:
        return False
    checks = [
        await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or "default",
            object_name=object_name,
            action=action,
        )
        for object_name, action in permissions
    ]
    return all(checks) if access_mode == "all" else any(checks)


async def can_access_page(
    db: AsyncSession, principal: AdminPrincipal, code: str
) -> bool:
    """检查页面权限。"""
    definition = await db.get(AdminPageDefinition, code)
    if definition is None or not definition.enabled:
        return False
    permissions = await _effective_page_permissions(
        db, page_code=code, tenant_id=principal.tenant_id
    )
    return await _permissions_allow(
        db, principal, permissions, definition.access_mode
    )


async def can_show_component(
    db: AsyncSession, principal: AdminPrincipal, page_code: str, component_code: str
) -> bool:
    """检查页面内组件显示权限。"""
    definition = await db.get(
        AdminComponentDefinition, (page_code, component_code)
    )
    if definition is None or not definition.enabled:
        return False
    permissions = await _effective_component_permissions(
        db,
        page_code=page_code,
        component_code=component_code,
        tenant_id=principal.tenant_id,
    )
    return await _permissions_allow(
        db, principal, permissions, definition.access_mode
    )


async def visible_pages(
    db: AsyncSession, principal: AdminPrincipal
) -> list[AdminPageDefinition]:
    """列出当前管理员可见的页面导航。"""
    rows = list(
        await db.scalars(
            sa.select(AdminPageDefinition)
            .where(AdminPageDefinition.enabled == 1)
            .order_by(
                AdminPageDefinition.nav_order.asc(),
                AdminPageDefinition.code.asc(),
            )
        )
    )
    visible = []
    for row in rows:
        if await can_access_page(db, principal, row.code):
            visible.append(row)
    return visible
