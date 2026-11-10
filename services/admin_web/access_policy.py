"""访问策略的共享校验、权限目录和角色投影。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.models.admin_control import CasbinRule


CRITICAL_PAGES = {
    "dashboard",
    "access_pages",
    "access_components",
    "platform_admins",
}

TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
ROLE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
ROLE_DOMAIN_RE = re.compile(
    r"^(?:\*|tenant:\*|tenant:[A-Za-z0-9][A-Za-z0-9._:-]{0,63})$"
)

# 内置角色被 OAuth 映射和默认策略引用；允许改权限，但不允许删除。
BUILTIN_ROLES = frozenset({"viewer", "operator", "tenant_admin"})

KNOWN_PERMISSIONS = {
    ("admin", "read"),
    ("admin_ui", "read"),
    ("admin_ui", "manage"),
    ("workflow", "read"),
    ("workflow", "write"),
    ("agent", "read"),
    ("agent", "write"),
    ("skill", "read"),
    ("skill", "write"),
    ("mcp", "read"),
    ("mcp", "write"),
    ("mcp", "refresh"),
    ("llm", "read"),
    ("llm", "write"),
    ("audit", "read"),
    ("admin_user", "read"),
    ("admin_user", "manage"),
    ("casbin", "role_read"),
    ("casbin", "role_manage"),
    ("casbin", "mapping_manage"),
    ("oauth_provider", "read"),
    ("oauth_provider", "manage"),
    ("cross_tenant", "read"),
    ("cross_tenant", "manage"),
    ("platform_admin", "owner"),
}

PLATFORM_ONLY_PERMISSIONS = frozenset(
    {
        ("cross_tenant", "read"),
        ("cross_tenant", "manage"),
        ("platform_admin", "owner"),
    }
)

PERMISSION_DESCRIPTIONS = {
    "admin": "Administration overview and appearance",
    "admin_ui": "Admin navigation and UI policy bindings",
    "workflow": "Workflow definitions and execution settings",
    "agent": "Agent configuration and connections",
    "skill": "Skill drafts, publishing and archives",
    "mcp": "MCP servers, tools and catalog refresh",
    "llm": "LLM endpoints and write-only credentials",
    "audit": "Tool-call audit records",
    "admin_user": "OAuth administrator identities",
    "casbin": "Roles, policy rules and OAuth mappings",
    "oauth_provider": "OIDC provider configuration",
    "cross_tenant": "Cross-tenant administration",
    "platform_admin": "Platform super-administrator ownership",
}


def permission_id(permission: tuple[str, str]) -> str:
    """把对象/操作权限转换成稳定的表单值。"""
    return f"{permission[0]}.{permission[1]}"


def parse_permission_values(values: list[str]) -> list[tuple[str, str]]:
    """解析复选框或逗号分隔的权限值，并拒绝未知权限。"""
    result: list[tuple[str, str]] = []
    for raw in values:
        for chunk in str(raw).replace("\n", ",").split(","):
            value = chunk.strip()
            if not value:
                continue
            if "." not in value:
                raise ValueError("invalid_permission")
            object_name, action = value.rsplit(".", 1)
            item = (object_name.strip(), action.strip())
            if item not in KNOWN_PERMISSIONS:
                raise ValueError("unknown_permission")
            result.append(item)
    return sorted(set(result))


def form_permissions(form: Any) -> list[tuple[str, str]]:
    """从表单读取复选框；兼容旧客户端的逗号输入框。"""
    values = [str(value) for value in form.getlist("permissions")]
    if not values and form.get("permissions") is not None:
        values = [str(form.get("permissions") or "")]
    return parse_permission_values(values)


def permission_groups(
    permissions: set[tuple[str, str]] | frozenset[tuple[str, str]] | None = None,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """按对象分组权限，供矩阵式复选框渲染。"""
    available = set(KNOWN_PERMISSIONS if permissions is None else permissions)
    grouped: dict[str, list[tuple[str, str]]] = {}
    for object_name, action in sorted(available):
        grouped.setdefault(object_name, []).append((object_name, action))
    return list(grouped.items())


@dataclass(frozen=True)
class RoleSummary:
    """角色在某个域名下的策略摘要。"""

    code: str
    domain: str
    permissions: tuple[tuple[str, str], ...]
    parents: tuple[str, ...]
    member_count: int
    mapping_count: int
    child_count: int

    @property
    def builtin(self) -> bool:
        """是否属于受保护的默认角色。"""
        return self.code in BUILTIN_ROLES

    @property
    def can_delete(self) -> bool:
        """是否满足删除保护条件。"""
        return not (
            self.builtin
            or self.member_count
            or self.mapping_count
            or self.child_count
        )


def role_summary_rows(
    rules: list[CasbinRule],
    *,
    mapping_counts: dict[str, int] | None = None,
) -> list[RoleSummary]:
    """把 Casbin p/g 规则投影为角色、权限、继承和引用计数。"""
    permissions: dict[tuple[str, str], set[tuple[str, str]]] = {}
    parents: dict[tuple[str, str], set[str]] = {}
    members: dict[tuple[str, str], int] = {}
    mappings = mapping_counts or {}
    for rule in rules:
        subject = rule.v0 or ""
        if rule.ptype == "p" and subject.startswith("role:"):
            key = (subject.removeprefix("role:"), rule.v1 or "tenant:*")
            permissions.setdefault(key, set()).add(
                (rule.v2 or "", rule.v3 or "")
            )
        elif rule.ptype == "g" and subject.startswith("role:"):
            key = (subject.removeprefix("role:"), rule.v2 or "tenant:*")
            parent = (rule.v1 or "").removeprefix("role:")
            if parent:
                parents.setdefault(key, set()).add(parent)
        elif rule.ptype == "g" and subject.startswith("user:"):
            parent = (rule.v1 or "").removeprefix("role:")
            key = (parent, rule.v2 or "tenant:*")
            if parent:
                members[key] = members.get(key, 0) + 1
    keys = set(permissions) | set(parents) | set(members)
    child_counts: dict[tuple[str, str], int] = {}
    for (_, domain), role_parents in parents.items():
        for parent in role_parents:
            key = (parent, domain)
            child_counts[key] = child_counts.get(key, 0) + 1
    return [
        RoleSummary(
            code=code,
            domain=domain,
            permissions=tuple(sorted(permissions.get((code, domain), set()))),
            parents=tuple(sorted(parents.get((code, domain), set()))),
            member_count=members.get((code, domain), 0),
            mapping_count=mappings.get(code, 0),
            child_count=child_counts.get((code, domain), 0),
        )
        for code, domain in sorted(keys)
    ]


def creates_role_cycle(
    role_code: str,
    domain: str,
    parents: list[str],
    all_rules: list[CasbinRule],
) -> bool:
    """判断继承变更是否形成环；域名不匹配的边不参与。"""
    graph: dict[str, set[str]] = {role_code: set(parents)}
    for rule in all_rules:
        if rule.ptype != "g" or not (rule.v0 or "").startswith("role:"):
            continue
        if (rule.v2 or "tenant:*") != domain:
            continue
        child = (rule.v0 or "").removeprefix("role:")
        parent = (rule.v1 or "").removeprefix("role:")
        if child and parent:
            graph.setdefault(child, set()).add(parent)

    visited: set[str] = set()
    active: set[str] = set()

    def visit(node: str) -> bool:
        """深度优先检测当前路径是否回到起点。"""
        if node in active:
            return node == role_code
        if node in visited:
            return False
        active.add(node)
        cycle = any(visit(parent) for parent in graph.get(node, set()))
        active.remove(node)
        visited.add(node)
        return cycle

    return visit(role_code)


def role_domains(roles: list[RoleSummary]) -> list[str]:
    """返回角色页面使用的域名列表。"""
    return sorted({role.domain for role in roles})


__all__ = [
    "BUILTIN_ROLES",
    "CRITICAL_PAGES",
    "KNOWN_PERMISSIONS",
    "PERMISSION_DESCRIPTIONS",
    "PLATFORM_ONLY_PERMISSIONS",
    "ROLE_CODE_RE",
    "ROLE_DOMAIN_RE",
    "RoleSummary",
    "TENANT_ID_RE",
    "creates_role_cycle",
    "form_permissions",
    "parse_permission_values",
    "permission_groups",
    "permission_id",
    "role_domains",
    "role_summary_rows",
]
