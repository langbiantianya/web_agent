"""渲染快照：页面结构与路由表不得在重构中悄悄改变。

比对的是元素树（见 ``tests/admin_snapshot.py``），不是 HTML 文本。
页面快照用**空数据集**渲染——重构会动的是页面骨架（表头、字段、按钮、
布局 class），数据行的渲染逻辑不在本次范围内，且已有测试覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from admin_snapshot import (
    SNAPSHOT_DIR,
    normalize,
    read_snapshot,
    route_table,
    write_snapshot,
)
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.app import app
from services.admin_web.views import (
    access_components_page,
    access_pages_page,
    agents_page,
    audit_page,
    dashboard,
    error_page,
    llm_page,
    login_page,
    mcp_page,
    mappings_page,
    oauth_providers_page,
    platform_admins_page,
    preferences_page,
    roles_page,
    skills_page,
    workflows_page,
)

PRINCIPAL = AdminPrincipal(
    kind="platform_super_admin",
    principal_id="owner",
    user_id=None,
    tenant_id=None,
    username="superAdmin",
    session_id=1,
    is_owner=True,
)

COMMON = {
    "principal": PRINCIPAL,
    "csrf": "csrf-token-for-snapshot",
    "pages": [],
    "theme": "fluent",
    "color_mode": "light",
    "density": "comfortable",
}

PAGE_FACTORIES = {
    "login_page": lambda: login_page(providers=[]),
    "dashboard": lambda: dashboard(
        **COMMON, counts={}, overview_visible=True, quick_actions_visible=True
    ),
    "workflows_page": lambda: workflows_page(
        **COMMON, rows=[], agents=[], can_write=True
    ),
    "agents_page": lambda: agents_page(**COMMON, rows=[], can_write=True),
    "skills_page": lambda: skills_page(**COMMON, rows=[], can_write=True),
    "mcp_page": lambda: mcp_page(
        **COMMON, rows=[], can_write=True, can_refresh=True
    ),
    "llm_page": lambda: llm_page(**COMMON, rows=[], can_write=True),
    "audit_page": lambda: audit_page(**COMMON, rows=[], total=0),
    "access_pages_page": lambda: access_pages_page(
        **COMMON, definitions=[], bindings={}
    ),
    "access_components_page": lambda: access_components_page(
        **COMMON, components=[], bindings={}
    ),
    "roles_page": lambda: roles_page(**COMMON, rules=[]),
    "mappings_page": lambda: mappings_page(
        **COMMON, mappings=[], providers=[]
    ),
    "oauth_providers_page": lambda: oauth_providers_page(
        **COMMON, providers=[], can_manage=True
    ),
    "platform_admins_page": lambda: platform_admins_page(**COMMON, accounts=[]),
    "preferences_page": lambda: preferences_page(**COMMON),
    "error_page": lambda: error_page(
        title_text="Forbidden", message="没有权限"
    ),
}


@pytest.mark.parametrize("name", sorted(PAGE_FACTORIES))
def test_page_structure_matches_snapshot(name: str) -> None:
    """页面的元素树必须与基线快照一致。"""
    path = SNAPSHOT_DIR / "pages" / f"{name}.json"
    actual = normalize(PAGE_FACTORIES[name]())
    if not path.exists():
        # 首次生成基线：写盘后立刻读回来比对，确保写的东西本身是有效的。
        write_snapshot(path, actual)
    expected = read_snapshot(path)
    assert actual == expected, (
        f"{name} 的渲染结构变了。差异明细：\n"
        f"{_diff(expected, actual)}"
    )


def test_route_table_matches_snapshot() -> None:
    """路由表（method + path + 顺序）必须与基线快照一致。"""
    path = SNAPSHOT_DIR / "routes.json"
    actual = route_table(app)
    if not path.exists():
        write_snapshot(path, actual)
    expected = read_snapshot(path)
    assert actual == expected, (
        f"路由表变了。差异明细：\n{_diff(expected, actual)}"
    )


def _diff(expected: Any, actual: Any) -> str:
    """给出可读的差异摘要（不追求完备，能指出第一处不同即可）。"""
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"长度 {len(expected)} → {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            if left != right:
                return f"第 {index} 项：\n  期望 {left}\n  实际 {right}"
        return "（逐项相同，但整体不等价）"
    return f"期望 {expected}\n实际 {actual}"
