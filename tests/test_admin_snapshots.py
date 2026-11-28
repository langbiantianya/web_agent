"""渲染快照：页面结构与路由表不得在重构中悄悄改变。

比对的是元素树（见 ``tests/admin_snapshot.py``），不是 HTML 文本。

**Ludic 视图层下线后（V4.10 阶段 E）**，Python 侧只剩三样渲染物：

- ``login_page``——登录页（会话与 CSRF 都在 Python 侧，必须由它渲染）；
- ``error_page``——未登录 / 无权限的兜底页；
- ``workflow_editor_page``——画布 island 的片段页。

业务页面的渲染已经全部交给 SvelteKit，所以这里**不再**有它们的页面
快照——那些快照随视图层一起退休了（见 ``docs/design/TODO.md`` 的阶段 E）。

**路由表快照仍然保留**，而且更重要了：它锁住了 ``admin-web`` 暴露的
全部端点（路径、方法、顺序）。Ludic 页面路由下线后，剩下的只有管理面
JSON API、登录/登出与画布 island 的三个端点——这个表就是那件事的守卫。

两套基线，缺一不可：

- **空数据集**（``tests/snapshots/admin/pages/*.json``）：无 provider、无
  节点、无 flash。只覆盖页面骨架。
- **非空数据集**（``tests/snapshots/admin/pages/with_data/*.json``）：带上
  一个 provider、一个带节点的画布与一条 flash。

环境变量 ``ADMIN_SNAPSHOT_UPDATE=1``：显式重写全部基线。默认路径**绝不
写盘**——基线缺失时测试直接失败并给出生成方法。
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
from services.admin_web.app import app
from services.admin_web.views import (
    error_page,
    login_page,
    workflow_editor_page,
)

# 显式更新基线的开关。只有设置了这个环境变量才会写盘；默认路径绝不写盘。
UPDATE_ENV = "ADMIN_SNAPSHOT_UPDATE"

# workflow_editor_page 的 workflow 参数是鸭子类型（只用到 code/name/
# description/version/enabled/definition），用最小假对象即可，不引真实模型、
# 不碰数据库。agents 传空列表，与「空数据集」同一前提。
WORKFLOW = SimpleNamespace(
    code="snapshot-workflow",
    name="Snapshot Workflow",
    description="快照用的最小工作流定义",
    version=1,
    enabled=True,
    definition={"nodes": [], "edges": []},
)

# 带节点的画布：definition 里有一个节点，agents 下拉里有一个 Agent。
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

AGENT_ITEM = SimpleNamespace(
    code="snapshot-agent",
    name="Snapshot Agent",
    enabled=True,
)

# login_page 的 providers：读 display_name/code（views/login.py）。
OAUTH_PROVIDER = SimpleNamespace(
    id=7,
    code="snapshot-idp",
    display_name="Snapshot IDP",
    issuer="https://id.example.com",
    client_id="snapshot-client",
    enabled=True,
)

FLASH = "Saved successfully."

# 画布片段页的参数：不再有 principal / pages / flash——片段页**没有后台
# 外壳**，外壳由 SvelteKit 那一层负责。
CANVAS_COMMON = {
    "csrf": "csrf-token-for-snapshot",
    "theme": "fluent",
    "color_mode": "light",
    "density": "comfortable",
}

PAGE_FACTORIES = {
    "login_page": lambda: login_page(providers=[]),
    "workflow_editor_page": lambda: workflow_editor_page(
        **CANVAS_COMMON, workflow=WORKFLOW, agents=[], can_write=True
    ),
    "error_page": lambda: error_page(
        title_text="Forbidden", message="没有权限"
    ),
}

PAGE_FACTORIES_WITH_DATA = {
    "login_page": lambda: login_page(
        providers=[OAUTH_PROVIDER], flash=FLASH
    ),
    "workflow_editor_page": lambda: workflow_editor_page(
        **{**CANVAS_COMMON, "csrf": "csrf-token-with-data"},
        workflow=WORKFLOW_WITH_NODES,
        agents=[AGENT_ITEM],
        can_write=True,
    ),
}
# error_page 没有数据参数（不接 flash），因此不纳入非空数据集基线——
# 它的渲染已被空数据集基线完整覆盖。

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

    路径形如 ``根节点.children[3].attrs.class``——实施者要靠它定位问题。
    所有路径统一以「根节点」起头，根层级的列表元素与长度差也带同一前缀。
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
