"""渲染快照：页面结构与路由表不得在重构中悄悄改变。

比对的是元素树（见 ``tests/admin_snapshot.py``），不是 HTML 文本。
页面快照用**空数据集**渲染——重构会动的是页面骨架（表头、字段、按钮、
布局 class），数据行的渲染逻辑不在本次范围内。

覆盖边界（事实陈述，不是「已经守住」的承诺）：
- ``tests/test_admin_web.py`` 对**数据行内**渲染零断言：per-row 编辑与
  操作标记（``Edit configuration``、``Save changes``、
  ``Leave blank to keep current key``、``row-actions``、``status-badge``）
  在整个既有测试文件里一次都没出现。
- 导航标记基本没有覆盖：``nav-link``、``nav-list`` 零断言，
  ``nav-group-title`` 只有一处断言（``tests/test_admin_web.py:674``）。
- flash 只有文案层面的断言（``tests/test_admin_web.py:1728`` 的
  ``Resource created.`` / ``Changes saved.``），标记与样式没有断言。
- 所以行内渲染、导航与 flash 标记是靠**本文件的快照**守住的，不是靠既有
  测试。快照只保证「空数据集下骨架不变」，数据行本身不在快照里。

覆盖 17 个页面（含 ``workflow_editor_page`` 编排画布页——Task 6 要搬它）。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    workflow_editor_page,
    workflows_page,
)

# 显式更新基线的开关。只有设置了这个环境变量才会写盘；默认路径绝不写盘。
UPDATE_ENV = "ADMIN_SNAPSHOT_UPDATE"

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

# workflow_editor_page 的 workflow 参数是鸭子类型（只用到 code / name /
# description / version / enabled / definition），用最小假对象即可，不引真实
# 模型、不碰数据库。agents 传空列表，与其它页面保持「空数据集」同一前提。
WORKFLOW = SimpleNamespace(
    code="snapshot-workflow",
    name="Snapshot Workflow",
    description="快照用的最小工作流定义",
    version=1,
    enabled=True,
    definition={"nodes": [], "edges": []},
)

PAGE_FACTORIES = {
    "login_page": lambda: login_page(providers=[]),
    "dashboard": lambda: dashboard(
        **COMMON, counts={}, overview_visible=True, quick_actions_visible=True
    ),
    "workflows_page": lambda: workflows_page(
        **COMMON, rows=[], agents=[], can_write=True
    ),
    "workflow_editor_page": lambda: workflow_editor_page(
        **COMMON, workflow=WORKFLOW, agents=[], can_write=True
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
    expected = _baseline(path, actual)
    assert actual == expected, (
        f"{name} 的渲染结构变了。差异明细：\n"
        f"{_diff(expected, actual)}"
    )


def test_route_table_matches_snapshot() -> None:
    """路由表（method + path + name + 顺序）必须与基线快照一致。"""
    path = SNAPSHOT_DIR / "routes.json"
    actual = route_table(app)
    expected = _baseline(path, actual)
    assert actual == expected, (
        f"路由表变了。差异明细：\n{_diff(expected, actual)}"
    )


def _update_requested() -> bool:
    """是否显式要求更新基线。

    默认永远为假——基线是重构前的真实输出，被误删后自动重建会让守卫凭空
    消失，那比重构本身引入的回归还危险。
    """
    return os.environ.get(UPDATE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _baseline(path: Path, actual: Any) -> Any:
    """取基线快照；缺失时默认直接失败，只有显式更新才写盘。"""
    if _update_requested():
        # 显式更新：始终写盘（新建或覆盖），用来重生成基线。
        write_snapshot(path, actual)
        return actual
    if not path.exists():
        pytest.fail(
            f"缺少基线快照：{path}\n"
            f"基线是重构前的真实输出，不应随手重建——自动重建会让守卫凭空"
            f"消失。若确认要重新生成基线，请显式执行：\n"
            f'  $env:{UPDATE_ENV} = "1"\n'
            f"  .venv\\Scripts\\python.exe -m pytest tests/test_admin_snapshots.py "
            f"-q -p pytest_asyncio.plugin -p pytest_timeout"
        )
    return read_snapshot(path)


def _diff(expected: Any, actual: Any) -> str:
    """给出可读的差异摘要：指出第一处不同（dict/list 递归到具体路径）。"""
    detail = _first_difference(expected, actual, "")
    if detail is not None:
        return detail
    return f"（逐项相同，但整体不等价）\n期望 {expected!r}\n实际 {actual!r}"


def _first_difference(expected: Any, actual: Any, path: str) -> str | None:
    """返回第一处差异的描述（含路径），没有差异时返回 None。

    路径形如 ``children[3].attrs.class``——Task 8 的实施者要靠它定位问题。
    """
    if expected == actual:
        return None
    where = path or "根节点"
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else str(key)
            if key not in expected:
                return f"{child}：基线里没有这一项，实际为 {actual[key]!r}"
            if key not in actual:
                return f"{child}：基线为 {expected[key]!r}，实际里没有这一项"
            detail = _first_difference(expected[key], actual[key], child)
            if detail is not None:
                return detail
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return f"{where}：长度 {len(expected)} → {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            detail = _first_difference(left, right, f"{path}[{index}]")
            if detail is not None:
                return detail
        return None
    return f"{where}：基线 {expected!r}，实际 {actual!r}"
