"""渲染快照：页面结构与路由表不得在重构中悄悄改变。

比对的是元素树（见 ``tests/admin_snapshot.py``），不是 HTML 文本。

两套基线，缺一不可：

- **空数据集**（``tests/snapshots/admin/pages/*.json``）：``pages=[]`` /
  ``rows=[]`` / 无 ``flash``。只覆盖页面骨架——表头、空态文案、创建表单、
  布局 class。这一套**不渲染** nav 链接（``nav-link`` / ``nav-group-title`` /
  ``nav-list``）、flash 提示条与**行内**编辑控件（``Edit configuration`` /
  ``Save changes`` / ``row-actions``）。
  （``workflow_editor_page`` 的 ``workflow`` 是必填单对象，不属于「数据集」，
  因此两套基线里它都有值；它工具栏上的 ``status-badge`` 与数据无关，空数据
  基线里也会出现。）
- **非空数据集**（``tests/snapshots/admin/pages/with_data/*.json``）：每个
  页面给一条代表性数据，并带上 nav 条目与一条 flash。覆盖 nav 链接、flash
  提示条（``flash`` / ``role="status"``）与行内编辑控件（``Edit
  configuration`` / ``Save changes`` / ``Leave blank to keep current key`` /
  ``row-actions`` / ``status-badge``）。
  两处**结构性例外**（不是漏覆盖）：``login_page`` 是独立登录页、不走
  ``shell()``，因此没有 nav；``dashboard`` 与 ``roles_page`` 的函数签名
  根本不接受 ``flash`` 参数，因此没有 flash。

两套合起来才是完整的页面守卫。既有测试 ``tests/test_admin_web.py`` 对这些
标记基本没有断言（只有 ``nav-group-title`` 一处、flash 两处文案），**不要**
把覆盖责任推给既有测试。

环境变量 ``ADMIN_SNAPSHOT_UPDATE=1``：显式重写全部基线。默认路径**绝不
写盘**——基线缺失时测试直接失败并给出生成方法。重写后请把它从 shell 里
清掉，否则此后每次运行都会重写基线（运行结束时会打印被重写的文件清单）。
"""

from __future__ import annotations

import os
from datetime import datetime
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
from app.models.admin_control import AdminPageDefinition
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

# workflow_editor_page 的 workflow 参数是鸭子类型（只用到 code/name/
# description/version/enabled/definition），用最小假对象即可，不引真实模型、
# 不碰数据库。agents 传空列表，与其它页面保持「空数据集」同一前提。
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

# ————————————————————————————————————————————————————————————
# 非空数据集：让 nav 链接、flash 提示条、行内编辑控件真正进入渲染。
#
# 假对象的字段集全部来自 services/admin_web/views.py 各页面函数的**实际
# 属性访问**（缺失字段会在渲染时直接 AttributeError 暴露，不会静默走降级
# 分支），取值刻意选成「非空」形态，避免 `xxx or ""` 之类的兜底把渲染拉到
# 降级分支而让基线失去守卫价值。
# ————————————————————————————————————————————————————————————

# nav 条目：_nav() 读 page.title / page.route_key / page.code / page.nav_group
# （views.py:103-139）。nav_group 取自 NAV_GROUP_ORDER 里的真实分组名。
NAV_PAGES = [
    AdminPageDefinition(
        code="overview",
        route_key="/admin/",
        title="Overview",
        nav_group="Overview",
    ),
    AdminPageDefinition(
        code="agents",
        route_key="/admin/agents",
        title="Agents",
        nav_group="Resources",
    ),
    AdminPageDefinition(
        code="audit",
        route_key="/admin/audit",
        title="Tool audit",
        nav_group="Security",
    ),
]

# 非空数据集用另一枚 CSRF 字面量：基线里若出现它，说明 CSRF 归一化失效。
WITH_DATA_COMMON = {
    "principal": PRINCIPAL,
    "csrf": "csrf-token-with-data",
    "pages": NAV_PAGES,
    "theme": "fluent",
    "color_mode": "light",
    "density": "comfortable",
}

FLASH = "Saved successfully."

# workflows_page 的 rows：读 row.code / row.name / row.enabled / row.version
# （views.py:604-627）；agents 读 a.code / a.name（views.py:638）。
WORKFLOW_ROW = SimpleNamespace(
    code="snapshot-workflow",
    name="Snapshot Workflow",
    enabled=True,
    version=2,
)
AGENT_ITEM = SimpleNamespace(
    code="snapshot-agent",
    name="Snapshot Agent",
    enabled=True,
)

# workflow_editor_page 的 workflow 读 code/name/description/version/enabled/
# definition（views.py:689-701），agents 读 code/name/enabled（views.py:698、
# 723-729）。
WORKFLOW_WITH_NODES = SimpleNamespace(
    code="snapshot-workflow",
    name="Snapshot Workflow",
    description="带节点的快照工作流",
    version=3,
    enabled=True,
    definition={
        "nodes": [{"id": "node-1", "kind": "agent", "agent": "snapshot-agent"}],
        "edges": [],
    },
)

# agents_page 的 rows：读 code/name/model/base_url/tools/skills/system_prompt/
# enabled/version（views.py:906-958）。
AGENT_ROW = SimpleNamespace(
    code="snapshot-agent",
    name="Snapshot Agent",
    model="gpt-4.1-mini",
    base_url="https://api.example.com/v1",
    tools=["web_search", "run_skill"],
    skills=["snapshot-skill"],
    system_prompt="You are a snapshot fixture.",
    enabled=True,
    version=3,
)

# skills_page 的 rows：读 code/name/status/description/body/enabled/version
# （views.py:1019-1076）。
SKILL_ROW = SimpleNamespace(
    code="snapshot-skill",
    name="Snapshot Skill",
    description="快照用的技能定义",
    status="published",
    body="Step 1. Do the thing.\n",
    enabled=True,
    version=2,
)

# mcp_page 的 rows：读 code/name/transport/timeout_sec/command/url/enabled/
# version（views.py:1135-1184）。
MCP_ROW = SimpleNamespace(
    code="snapshot-mcp",
    name="Snapshot MCP",
    transport="http",
    timeout_sec=30,
    command="",
    url="https://mcp.example.com",
    enabled=True,
    version=4,
)

# llm_page 的 rows：读 code/name/base_url/priority/enabled（views.py:1243-1286）。
LLM_ROW = SimpleNamespace(
    code="snapshot-llm",
    name="Snapshot LLM",
    base_url="https://llm.example.com/v1",
    priority=10,
    enabled=True,
    version=1,
)

# audit_page 的 rows：读 id/run_id/tool_name/node_id/source/ok/failure_type/
# duration_ms/created_at（views.py:1343-1353）。
AUDIT_ROW = SimpleNamespace(
    id=1,
    run_id=42,
    tool_name="web_search",
    node_id="node-1",
    source="builtin",
    ok=True,
    failure_type=None,
    duration_ms=123,
    created_at=datetime(2026, 1, 2, 3, 4, 5),
)

# oauth_providers_page / login_page / mappings_page 的 providers：读
# display_name/code/id（views.py:235-246、1720-1721、2029-2085）以及
# _provider_form() 读的那一组字段（views.py:1801-2013）。
OAUTH_PROVIDER = SimpleNamespace(
    id=7,
    code="snapshot-idp",
    display_name="Snapshot IDP",
    issuer="https://id.example.com",
    discovery_url="https://id.example.com/.well-known/openid-configuration",
    authorization_endpoint="https://id.example.com/authorize",
    token_endpoint="https://id.example.com/token",
    jwks_uri="https://id.example.com/jwks",
    client_id="snapshot-client",
    client_secret_cipher="encrypted-secret",
    token_auth_method="client_secret_basic",
    scopes="openid profile email",
    username_claim="preferred_username",
    role_claim="groups",
    role_claim_required=True,
    role_sync_max_age_seconds=1800,
    auto_create=True,
    client_login_enabled=True,
    admin_login_enabled=True,
    enabled=True,
)

# mappings_page 的 mappings：读 provider_id/claim_name/external_value/
# role_code/enabled（views.py:1723-1731）。
ROLE_MAPPING_ROW = SimpleNamespace(
    provider_id=7,
    claim_name="groups",
    external_value="platform-admins",
    role_code="oauth_admin",
    enabled=True,
)

# roles_page 的 rules：读 ptype/v0/v1/v2/v3（views.py:1669-1677）。
CASBIN_RULE = SimpleNamespace(
    ptype="p",
    v0="oauth_admin",
    v1="workflow",
    v2="read",
    v3="",
)

# platform_admins_page 的 accounts：读 username/email/phone/status/
# password_expires_at/owner_slot/id（views.py:2120-2144）。两条分别覆盖
# owner 徽标分支与删除表单分支。
PLATFORM_ADMIN_OWNER = SimpleNamespace(
    id=3,
    username="snapshotOwner",
    email="owner@example.com",
    phone="+8613800000000",
    status="active",
    owner_slot="superAdmin",
    password_expires_at=datetime(2026, 6, 1, 0, 0, 0),
)
PLATFORM_ADMIN_PLAIN = SimpleNamespace(
    id=4,
    username="snapshotAdmin",
    email=None,
    phone=None,
    status="active",
    owner_slot=None,
    password_expires_at=None,
)

# access_pages_page 的 definitions（真实模型，_nav 同款）：读 title/code/
# nav_group（views.py:1444-1461）。
ACCESS_PAGE_DEFINITION = AdminPageDefinition(
    code="agents",
    route_key="/admin/agents",
    title="Agents",
    nav_group="Resources",
)

# access_components_page 的 components：读 page_code/code/label/kind
# （views.py:1602-1627）。
ADMIN_COMPONENT = SimpleNamespace(
    page_code="agents",
    code="agent-table",
    label="Agent table",
    kind="table",
)

# 权限行：_permission_editor 读 binding_scope/object_name/action_name，
# 并读 scoped_rows[0].tenant_id（views.py:1522-1554）。
PERMISSION_ROW = SimpleNamespace(
    binding_scope="global",
    object_name="workflow",
    action_name="read",
    tenant_id=None,
)

PAGE_FACTORIES_WITH_DATA = {
    "login_page": lambda: login_page(
        providers=[OAUTH_PROVIDER], flash=FLASH
    ),
    "dashboard": lambda: dashboard(
        **WITH_DATA_COMMON,
        counts={
            "conversations": 12,
            "runs": 34,
            "admins": 2,
            "platform_admins": 1,
        },
        overview_visible=True,
        quick_actions_visible=True,
    ),
    "workflows_page": lambda: workflows_page(
        **WITH_DATA_COMMON,
        rows=[WORKFLOW_ROW],
        agents=[AGENT_ITEM],
        can_write=True,
        flash=FLASH,
    ),
    "workflow_editor_page": lambda: workflow_editor_page(
        **WITH_DATA_COMMON,
        workflow=WORKFLOW_WITH_NODES,
        agents=[AGENT_ITEM],
        can_write=True,
        flash=FLASH,
    ),
    "agents_page": lambda: agents_page(
        **WITH_DATA_COMMON, rows=[AGENT_ROW], can_write=True, flash=FLASH
    ),
    "skills_page": lambda: skills_page(
        **WITH_DATA_COMMON, rows=[SKILL_ROW], can_write=True, flash=FLASH
    ),
    "mcp_page": lambda: mcp_page(
        **WITH_DATA_COMMON,
        rows=[MCP_ROW],
        can_write=True,
        can_refresh=True,
        flash=FLASH,
    ),
    "llm_page": lambda: llm_page(
        **WITH_DATA_COMMON, rows=[LLM_ROW], can_write=True, flash=FLASH
    ),
    "audit_page": lambda: audit_page(
        **WITH_DATA_COMMON, rows=[AUDIT_ROW], total=1, flash=FLASH
    ),
    "access_pages_page": lambda: access_pages_page(
        **WITH_DATA_COMMON,
        definitions=[ACCESS_PAGE_DEFINITION],
        bindings={"agents": [PERMISSION_ROW]},
        flash=FLASH,
    ),
    "access_components_page": lambda: access_components_page(
        **WITH_DATA_COMMON,
        components=[ADMIN_COMPONENT],
        bindings={("agents", "agent-table"): [PERMISSION_ROW]},
        flash=FLASH,
    ),
    "roles_page": lambda: roles_page(
        **WITH_DATA_COMMON, rules=[CASBIN_RULE]
    ),
    "mappings_page": lambda: mappings_page(
        **WITH_DATA_COMMON,
        mappings=[ROLE_MAPPING_ROW],
        providers=[OAUTH_PROVIDER],
        flash=FLASH,
    ),
    "oauth_providers_page": lambda: oauth_providers_page(
        **WITH_DATA_COMMON,
        providers=[OAUTH_PROVIDER],
        can_manage=True,
        flash=FLASH,
    ),
    "platform_admins_page": lambda: platform_admins_page(
        **WITH_DATA_COMMON,
        accounts=[PLATFORM_ADMIN_OWNER, PLATFORM_ADMIN_PLAIN],
        flash=FLASH,
    ),
    "preferences_page": lambda: preferences_page(
        **WITH_DATA_COMMON, flash=FLASH
    ),
}
# error_page 没有数据参数（不接 pages / flash，也不走 shell），因此不纳入
# 非空数据集基线——它的渲染已被空数据集基线完整覆盖。

# 本轮被重写过的基线文件（仅更新模式下非空），运行结束时统一打印。
_REWRITTEN: list[Path] = []


@pytest.mark.parametrize("name", sorted(PAGE_FACTORIES))
def test_page_structure_matches_snapshot(name: str) -> None:
    """空数据集下页面的元素树必须与基线快照一致。"""
    path = SNAPSHOT_DIR / "pages" / f"{name}.json"
    actual = normalize(PAGE_FACTORIES[name]())
    expected = _baseline(path, actual)
    assert actual == expected, (
        f"[空数据集] {name} 的渲染结构变了。差异明细：\n"
        f"{_diff(expected, actual)}"
    )


@pytest.mark.parametrize("name", sorted(PAGE_FACTORIES_WITH_DATA))
def test_page_structure_with_data_matches_snapshot(name: str) -> None:
    """非空数据集下页面的元素树必须与基线快照一致。"""
    path = SNAPSHOT_DIR / "pages" / "with_data" / f"{name}.json"
    actual = normalize(PAGE_FACTORIES_WITH_DATA[name]())
    expected = _baseline(path, actual)
    assert actual == expected, (
        f"[非空数据集] {name} 的渲染结构变了。差异明细：\n"
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


@pytest.fixture(scope="session", autouse=True)
def _report_rewritten_baselines(pytestconfig: pytest.Config) -> None:
    """注册终端摘要钩子，打印本轮被重写的基线文件清单。

    更新模式会静默改写基线，不打印就看不出这一轮到底动了哪些文件。
    钩子必须等会话结束才触发，所以在 fixture 里动态注册插件——测试模块里
    直接写 ``pytest_terminal_summary`` 不会被 pytest 收集。
    """

    class _RewrittenReporter:
        def pytest_terminal_summary(
            self, terminalreporter: Any, exitstatus: Any, config: Any
        ) -> None:
            if not _REWRITTEN:
                return
            terminalreporter.write_sep("=", "已重写的快照基线")
            for path in _REWRITTEN:
                terminalreporter.write_line(f"  {path}")

    pytestconfig.pluginmanager.register(
        _RewrittenReporter(), "admin-snapshot-rewritten-report"
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
    """取基线快照；缺失时默认直接失败，只有显式更新才写盘。

    更新模式下写盘后**回读文件**再返回：比对的是「渲染结果」与「文件里
    的内容」两个独立对象，写坏了、写漏了都会当场暴露，而不是自己跟自己
    比一遍恒绿。
    """
    if _update_requested():
        write_snapshot(path, actual)
        _REWRITTEN.append(path)
        return read_snapshot(path)
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
    detail = _first_difference(expected, actual, "根节点")
    if detail is not None:
        return detail
    return f"（逐项相同，但整体不等价）\n期望 {expected!r}\n实际 {actual!r}"


def _first_difference(expected: Any, actual: Any, path: str) -> str | None:
    """返回第一处差异的描述（含路径），没有差异时返回 None。

    路径形如 ``根节点.children[3].attrs.class``——Task 8 的实施者要靠它
    定位问题。所有路径统一以「根节点」起头，根层级的列表元素与长度差也
    带同一前缀。
    """
    if expected == actual:
        return None
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}"
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
            return f"{path}：长度 {len(expected)} → {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            detail = _first_difference(left, right, f"{path}[{index}]")
            if detail is not None:
                return detail
        return None
    return f"{path}：基线 {expected!r}，实际 {actual!r}"
