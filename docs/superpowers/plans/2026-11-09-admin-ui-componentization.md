# 管理端 UI 组件化与模块拆分 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `services/admin_web/app.py`（3373 行）与 `views.py`（2303 行）拆成 `routes/`（12 个域模块）与 `views/`（15 个模块）两个按业务域切分的包，并把表单字段改为数据驱动、消除创建/编辑双写，同时建立渲染快照防止重构悄悄改变页面结构。

**Architecture:** 视图层与路由层分成两个平行的包，一一对应同名业务域模块；路由模块用 `register(app)` 暴露、装饰器写法保持不变，注册顺序固化在 `routes/__init__.py` 的一个元组里；表单字段抽成 `Field` 描述，创建与编辑共用一份定义。渲染回归保护走 **Python 元素树快照**（不是 HTML 文本解析），零新依赖。

**Tech Stack:** Python 3.14 / Starlette（`LudicApp` 是其子类）/ Ludic SSR / pytest / `node --test`

**设计文档：** `docs/superpowers/specs/2026-11-09-admin-ui-componentization-design.md`

## Global Constraints

这些约束来自设计文档，**每个 Task 都隐含包含**：

- **docstring 与注释一律中文**；新模块带模块级说明，写清「为什么」而不只是「是什么」。
- **不改 class 名**。`admin.css` 有 371 个 class 选择器，重命名收益远小于样式回归风险。快照会捕捉 class 值变化并判为差异——这是技术兜底。
- **不改业务逻辑、错误文案、权限判定、CSRF 与 Origin 校验链路**。
- **不引入第三方依赖**：不新增 `package.json`、不引 npm 包、不用 CDN、不加构建步骤、不新增数据库迁移。
- **不碰 `app/core/workflow_def.py`**（图规则唯一实现）与 V4.8 的编排页改动。
- **不碰 `admin.css`**。
- **路由注册顺序是行为的一部分**：Starlette 首个匹配 path 的路由胜出。
- **搬家的定位方式**：计划里各「搬入函数」清单带的历史行号取自**原始 `views.py`**，会随前序搬家整体位移
  （Task 3 评审就查出过 Task 4 的行号失准）。**函数名才是准的**——按名字定位，不要按行号定位。
- **既有测试的红线仍成立**：V4.8 之前的测试一个字节都不许改。
- 验证命令统一为：
  ```powershell
  cd C:\Users\lbty\project\web_agent
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_web.py -q -p pytest_asyncio.plugin -p pytest_timeout   # 期望 43
  .venv\Scripts\python.exe -m pytest tests/test_admin_snapshots.py -q -p pytest_asyncio.plugin -p pytest_timeout
  .venv\Scripts\python.exe -m pytest tests/test_admin_structure.py -q -p pytest_asyncio.plugin -p pytest_timeout
  node --test tests/js/workflow-editor-core.test.mjs                                                          # 期望 22/22
  node --test tests/js/workflow-editor.test.mjs                                                               # 期望 52/52
  ```

**基线数字（2026-11-09 实测）：** `test_admin_web` 43 passed、`test_workflow_def` 39、`test_api_e2e` 15、`test_mysql_schema_compat` 3、`test_admin_entrypoint` 12；JS core 22/22、editor 52/52。

---

## File Structure

| 文件 | 职责 | 行数目标 |
| --- | --- | --- |
| `services/admin_web/app.py` | 应用装配：创建 app、lifespan、中间件、静态挂载、调 `register_all` | ~150 |
| `services/admin_web/common.py` | 跨域共用的私有辅助（原 `app.py` 内的 `_` 前缀函数） | ~250 |
| `services/admin_web/routes/__init__.py` | `register_all(app)`，固化注册顺序 | ~40 |
| `services/admin_web/routes/{auth,dashboard,workflows,agents,skills,mcp,llm,audit,oauth_providers,access,platform_admins,preferences}.py` | 各业务域的路由 | 各 ≤300 |
| `services/admin_web/views/__init__.py` | 视图再导出（保持 `from services.admin_web.views import X` 可用） | ~60 |
| `services/admin_web/views/shell.py` | `shell` `_flash` `_nav` `_locale_switch` `error_page` + 常量 | ~250 |
| `services/admin_web/views/components.py` | `_metric` `_csrf` `_toggle_and_delete` `_management_page` | ~150 |
| `services/admin_web/views/fields.py` | `Field` 描述与表单渲染 | ~200 |
| `services/admin_web/views/{login,dashboard,workflows,agents,skills,mcp,llm,audit,oauth_providers,access,platform_admins,preferences}.py` | 各业务页面 | 各 ≤300 |
| `tests/admin_snapshot.py` | 元素树归一化、路由表提取、快照读写 | ~130 |
| `tests/test_admin_snapshots.py` | 页面结构快照（空/非空两套）+ 路由表快照 | ~600 |
| `tests/test_admin_structure.py` | 包结构契约（模块清单、导出、单文件行数上限） | ~90 |
| `tests/test_admin_fields.py` | `Field` 与表单渲染的单元测试 | ~140 |
| `tests/snapshots/admin/routes.json` | 路由表基线（含 route name） | — |
| `tests/snapshots/admin/pages/*.json` | 17 个页面的元素树基线（空数据集） | — |
| `tests/snapshots/admin/pages/with_data/*.json` | 16 个页面的元素树基线（非空数据集：一条代表性行 + nav 条目 + 一条 flash） | — |

---

### Task 1: 渲染快照基建与基线抓取

**Files:**
- Create: `tests/admin_snapshot.py`
- Create: `tests/test_admin_snapshots.py`
- Create: `tests/snapshots/admin/routes.json`（由测试生成）
- Create: `tests/snapshots/admin/pages/*.json`（由测试生成）

**Interfaces:**
- Consumes: `services.admin_web.app.app`（现成应用对象）、`services.admin_web.views.*`（16 个页面渲染函数）
- Produces:
  - `admin_snapshot.normalize(node: Any) -> Any` —— 元素树 → 可 JSON 序列化的结构
  - `admin_snapshot.route_table(app: Any) -> list[dict]` —— 路由表（method + path + 顺序）
  - `admin_snapshot.write_snapshot(path: Path, payload: Any) -> None`
  - `admin_snapshot.read_snapshot(path: Path) -> Any`
  - `admin_snapshot.SNAPSHOT_DIR: Path` —— `tests/snapshots/admin`

- [ ] **Step 1: 写归一化工具**

  创建 `tests/admin_snapshot.py`：

  ```python
  """管理端页面的渲染快照工具。

  比对的是 **Python 元素树** 而不是 HTML 文本：Ludic 渲染出的就是元素对象，
  在 Python 层比对比解析 HTML 字符串精确得多，而且不需要任何第三方解析库。

  结构等价的定义：
  - 属性**顺序**变化不算差异（比对前排序）；
  - 标签、属性值、文本、children 结构变化算差异；
  - class 名变化**算**差异——这是「不改 class 名」的技术兜底。
  """

  from __future__ import annotations

  import json
  import re
  from pathlib import Path
  from typing import Any

  SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots" / "admin"

  # CSRF token 与时间戳每次渲染都不同，必须归一化，否则快照必然失败。
  CSRF_PLACEHOLDER = "<CSRF>"
  TIMESTAMP_PLACEHOLDER = "<TS>"

  _TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?")


  def _normalize_text(text: str) -> str:
      """把文本里的时间戳换成占位符。"""
      return _TIMESTAMP_RE.sub(TIMESTAMP_PLACEHOLDER, text)


  def _normalize_value(key: str, value: Any) -> Any:
      """按属性名与值类型归一化。"""
      if key == "csrf_token":
          return CSRF_PLACEHOLDER
      if isinstance(value, str):
          return _normalize_text(value)
      return value


  def normalize(node: Any) -> Any:
      """把元素（或字符串子节点）转成可 JSON 序列化的结构。

      标签名取 ``html_name``；属性取 ``aliased_attrs``（HTML 名，Python 侧的
      ``class_`` 在那里是 ``class``）并排序；``children`` 是元素与字符串的
      混排元组，分别处理。
      """
      if isinstance(node, str):
          return {"text": _normalize_text(node)}
      attrs = {
          key: _normalize_value(key, value)
          for key, value in sorted(getattr(node, "aliased_attrs", {}).items())
      }
      return {
          "tag": getattr(node, "html_name", type(node).__name__),
          "attrs": attrs,
          "children": [normalize(child) for child in getattr(node, "children", ())],
      }


  def route_table(app: Any) -> list[dict]:
      """应用的路由表：method + path + **顺序**。

      顺序是行为的一部分（Starlette 首个匹配 path 的路由胜出），所以这里
      保序而不是排序。
      """
      table: list[dict] = []
      for route in getattr(app, "routes", []):
          path = getattr(route, "path", None)
          if not path:
              continue
          methods = sorted(getattr(route, "methods", None) or [])
          table.append({"path": path, "methods": methods})
      return table


  def write_snapshot(path: Path, payload: Any) -> None:
      """写快照文件（缩进 + 排序键，便于 review diff）。"""
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_text(
          json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
          encoding="utf-8",
      )


  def read_snapshot(path: Path) -> Any:
      """读快照文件。"""
      return json.loads(path.read_text(encoding="utf-8"))
  ```

- [ ] **Step 2: 写快照测试**

  创建 `tests/test_admin_snapshots.py`：

  ```python
  """渲染快照：页面结构与路由表不得在重构中悄悄改变。

  比对的是元素树（见 ``tests/admin_snapshot.py``），不是 HTML 文本。
  页面快照用**空数据集**渲染——重构会动的是页面骨架（表头、字段、按钮、
  布局 class）。
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
  ```

  **注意**：`from admin_snapshot import ...` 是平铺导入——`tests/` 不是 Python 包
  （没有 `__init__.py`），但 `tests/conftest.py` 把仓库根插入 `sys.path`，
  pytest 默认又把测试文件所在目录插入 `sys.path`，因此 `tests/admin_snapshot.py`
  可以被平铺导入。

- [ ] **Step 3: 运行测试，生成基线快照**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_snapshots.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 34 passed（17 个空数据集页面 + 16 个非空数据集页面 + 1 个路由表），
  并生成 `tests/snapshots/admin/routes.json`、`tests/snapshots/admin/pages/` 下 17 个文件，
  以及 `tests/snapshots/admin/pages/with_data/` 下 16 个文件。

  > **执行修订（2026-11-09，Task 1 完成后回写）**：原计划只做「空数据集」一套基线，
  > 理由是「数据行的渲染逻辑不在本次范围内，且已有测试覆盖」。复审核实后半句**不成立**——
  > `tests/test_admin_web.py` 对 `row-actions` / `status-badge` / `Edit configuration` /
  > `Save changes` / `Leave blank to keep current key` / `nav-link` / `nav-list` /
  > flash 文案零断言。空数据集渲染（`pages=[]` / `rows=[]` / 无 `flash`）使这些分支
  > **根本不被渲染**，而 Task 8「消除创建/编辑双写」改的正是行内编辑表单——
  > 重构改动最狠的地方恰好没有守卫。故追加「非空数据集」一套基线：每页给一条代表性数据
  > （假对象字段集按 `views.py` 各页面函数的实际属性访问构造），并带上 nav 条目与一条 flash。
  > `error_page` 不纳入非空基线（无数据参数、不走 `shell()`，非空版只会是空数据版的副本）。

- [ ] **Step 4: 确认基线快照已入库且内容合理**

  Run:
  ```powershell
  Get-ChildItem tests\snapshots\admin -Recurse -File | Select-Object Name, Length
  .venv\Scripts\python.exe -c "import json;d=json.load(open('tests/snapshots/admin/routes.json',encoding='utf-8'));print(len(d),'条路由');print([r['path'] for r in d][:6])"
  ```

  Expected: 34 个文件；路由条目数 > 50（现有约 50 个路由 + 静态挂载）；
  前 6 条路径应是 `/admin/static`（静态挂载，先于装饰器注册）、
  `/admin/health`、`/admin/login`、`/admin/login/super-admin`、
  `/admin/login/oauth/{provider_code}`、`/admin/auth/oauth/{provider_code}/callback`
  —— 与现有注册顺序一致。

- [ ] **Step 5: 提交**

  ```bash
  git add tests/admin_snapshot.py tests/test_admin_snapshots.py tests/snapshots/
  git commit -m "test(admin): 建立渲染快照与路由表基线" -m "比对的是 Python 元素树而不是 HTML 文本：Ludic 渲染出的就是元素对象，Python 层比对更精确且零新依赖。CSRF token 与时间戳归一化成占位符，否则每次渲染都不同。" -m "路由表快照保序——Starlette 首个匹配 path 的路由胜出，顺序是行为的一部分。"
  ```

---

### Task 2: 新包骨架与结构契约测试

**Files:**
- Rename: `services/admin_web/views.py` → `services/admin_web/views_legacy.py`（**改名让位**，见 Step 3）
- Create: `services/admin_web/views/__init__.py`
- Create: `services/admin_web/routes/__init__.py`
- Create: `tests/test_admin_structure.py`

**Interfaces:**
- Consumes: 无（此时是纯新增）
- Produces:
  - `services.admin_web.views` 包（此时只有 docstring，后续 Task 逐个填充）
  - `services.admin_web.routes` 包，含 `register_all(app) -> None`

- [ ] **Step 1: 写结构契约测试**

  创建 `tests/test_admin_structure.py`：

  ```python
  """管理端包结构契约。

  重构的目标是「按业务域拆成两个包」，这个测试把目标本身钉住：
  模块清单、导出清单、单文件行数上限，都不允许在重构中途悄悄偏离。
  """

  from __future__ import annotations

  import inspect
  from pathlib import Path

  import pytest

  ROOT = Path(__file__).resolve().parents[1]
  ADMIN = ROOT / "services" / "admin_web"

  VIEW_MODULES = (
      "shell",
      "components",
      "fields",
      "login",
      "dashboard",
      "workflows",
      "agents",
      "skills",
      "mcp",
      "llm",
      "audit",
      "oauth_providers",
      "access",
      "platform_admins",
      "preferences",
  )

  ROUTE_MODULES = (
      "auth",
      "dashboard",
      "workflows",
      "agents",
      "skills",
      "mcp",
      "llm",
      "audit",
      "oauth_providers",
      "access",
      "platform_admins",
      "preferences",
  )

  # 单文件行数上限。设计文档的目标是「任何单文件不超过 350 行」。
  MAX_MODULE_LINES = 350
  MAX_APP_LINES = 200


  @pytest.mark.parametrize("name", VIEW_MODULES)
  def test_views_module_exists(name: str) -> None:
      """视图包下每个业务域都要有独立模块。"""
      assert (ADMIN / "views" / f"{name}.py").is_file()


  @pytest.mark.parametrize("name", ROUTE_MODULES)
  def test_route_module_exists(name: str) -> None:
      """路由包下每个业务域都要有独立模块。"""
      assert (ADMIN / "routes" / f"{name}.py").is_file()


  def test_legacy_views_module_is_gone() -> None:
      """过渡期的 views_legacy.py 必须被删掉——留着它就等于没拆完。"""
      assert not (ADMIN / "views_legacy.py").exists()


  def test_views_package_still_exports_every_page() -> None:
      """改名让位期间，视图包必须仍然导出全部页面函数。

      拆包是**逐步**进行的：中途任何一步都不能让
      ``from services.admin_web.views import X`` 失效——那会让整个管理端
      起不来，43 个既有测试当场全红。
      """
      import services.admin_web.views as views

      for name in (
          "login_page",
          "dashboard",
          "workflows_page",
          "workflow_editor_page",
          "agents_page",
          "skills_page",
          "mcp_page",
          "llm_page",
          "audit_page",
          "access_pages_page",
          "access_components_page",
          "roles_page",
          "mappings_page",
          "oauth_providers_page",
          "platform_admins_page",
          "preferences_page",
          "error_page",
          "shell",
      ):
          assert callable(getattr(views, name, None)), name


  @pytest.mark.parametrize(
      "path",
      [
          *(ADMIN / "views" / f"{name}.py" for name in VIEW_MODULES),
          *(ADMIN / "routes" / f"{name}.py" for name in ROUTE_MODULES),
      ],
  )
  def test_module_stays_small(path: Path) -> None:
      """任何单文件不得超过行数上限。"""
      lines = len(path.read_text(encoding="utf-8").splitlines())
      assert lines <= MAX_MODULE_LINES, f"{path.name} 有 {lines} 行，超出上限"


  def test_app_module_is_small() -> None:
      """app.py 只剩应用装配。"""
      lines = len((ADMIN / "app.py").read_text(encoding="utf-8").splitlines())
      assert lines <= MAX_APP_LINES, f"app.py 有 {lines} 行，超出上限"


  def test_routes_expose_register() -> None:
      """每个路由模块都要暴露 register(app)。"""
      for name in ROUTE_MODULES:
          module = __import__(
              f"services.admin_web.routes.{name}", fromlist=["register"]
          )
          assert callable(getattr(module, "register", None)), name


  def test_register_all_is_callable() -> None:
      """routes 包导出 register_all。"""
      module = __import__("services.admin_web.routes", fromlist=["register_all"])
      assert callable(getattr(module, "register_all", None))
  ```

- [ ] **Step 2: 运行测试，确认失败**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_structure.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 大量 FAIL（`views/*.py` 与 `routes/*.py` 还不存在，
  `legacy_views_module_is_gone` 也会失败，因为 `views_legacy.py` 还在）。

- [ ] **Step 3: 改名让位，再建包**

  **先改名，再建包**——顺序不能反。CPython 解析同名模块与包时**包优先**：
  一旦 `views/` 目录里有了 `__init__.py`，`from services.admin_web.views import X`
  就改指向包而不是同名模块。所以必须先把旧文件挪开（改名为 `views_legacy.py`）。

  ```bash
  git mv services/admin_web/views.py services/admin_web/views_legacy.py
  ```

  然后创建 `services/admin_web/views/__init__.py`：

  ```python
  """管理端页面渲染包。

  按业务域拆成独立模块：``shell``（外壳与全局片段）、``components``
  （通用 UI 组件）、``fields``（数据驱动的表单字段），以及每个业务域一个
  页面模块。

  这里做再导出，是为了保持 ``from services.admin_web.views import
  workflows_page`` 这类调用点可用——调用方不需要知道页面住在哪个模块。

  拆分是逐步进行的：还没搬走的页面暂时从 ``views_legacy`` 再导出，
  全部搬完后（Task 7）删掉那个过渡模块。
  """

  from services.admin_web.views_legacy import (
      access_components_page,
      access_pages_page,
      agents_page,
      audit_page,
      dashboard,
      error_page,
      llm_page,
      login_page,
      mappings_page,
      mcp_page,
      oauth_providers_page,
      platform_admins_page,
      preferences_page,
      roles_page,
      shell,
      skills_page,
      workflow_editor_page,
      workflows_page,
  )
  ```

  > **执行修订（2026-11-09）**：原清单漏了 `shell`，而
  > `test_views_package_still_exports_every_page` 明确要求 `shell` 可从
  > `services.admin_web.views` 取到（`views.py` 里有 18 个公开页面/外壳函数，
  > 再导出必须覆盖全部 18 个）。

  创建 `services/admin_web/routes/__init__.py`：

  ```python
  """管理端路由包。

  每个业务域一个模块，模块暴露 ``register(app)``；``register_all`` 负责
  按**固定顺序**注册全部路由。

  顺序是行为的一部分：Starlette 首个匹配 path 的路由胜出。域模块不 import
  app 对象（由参数传入），避免与 ``services.admin_web.app`` 形成循环导入。
  """

  from __future__ import annotations

  from typing import Any


  def register_all(app: Any) -> None:
      """按重构前的装饰器顺序注册各域路由。

      顺序必须与重构前**逐条一致**——``tests/snapshots/admin/routes.json``
      会把顺序一起比对，改动顺序会让那条测试变红。
      """
      from services.admin_web.routes import (
          access,
          agents,
          audit,
          auth,
          dashboard,
          llm,
          mcp,
          oauth_providers,
          platform_admins,
          preferences,
          skills,
          workflows,
      )

      for module in (
          auth,
          dashboard,
          workflows,
          agents,
          skills,
          mcp,
          llm,
          audit,
          oauth_providers,
          access,
          platform_admins,
          preferences,
      ):
          module.register(app)
  ```

- [ ] **Step 4: 运行测试，确认只剩「模块还没建」类的失败**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_structure.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: `test_legacy_views_module_is_gone` 与 `test_app_module_is_small` 仍然 FAIL
  （预期如此，要到 Task 7 / Task 9 才可能通过）；`test_views_package_still_exports_every_page`
  应当**通过**（改名让位的安全契约：中途任何一步都不能让
  `from services.admin_web.views import X` 失效）；其余应当仍然 FAIL，
  因为业务域模块还没建。**本任务不要求这个测试全绿**——它是后续任务的
  RED 契约。

- [ ] **Step 5: 提交**

  ```bash
  git add services/admin_web/views/__init__.py services/admin_web/routes/__init__.py tests/test_admin_structure.py
  git commit -m "test(admin): 建立包结构契约测试" -m "把重构目标本身钉住：视图包与路由包的模块清单、每个路由模块都要暴露 register(app)、单文件行数上限、app.py 只剩装配。测试此刻是红的——它就是后续搬家任务的验收契约。"
  ```

---

### Task 3: views/shell.py —— 外壳与全局片段

**Files:**
- Create: `services/admin_web/views/shell.py`
- Modify: `services/admin_web/views_legacy.py`（删掉已搬走的函数，改为从新模块导入）
- Modify: `services/admin_web/views/__init__.py`（再导出）
- Test: `tests/test_admin_structure.py`、`tests/test_admin_snapshots.py`

**Interfaces:**
- Consumes: `services.admin_web.accounts.AdminPrincipal`、`app.models.admin_control.AdminPageDefinition`
- Produces（`services.admin_web.views.shell`）：
  - `ADMIN_STYLESHEET: str`
  - `ADMIN_UI_SCRIPT: str`
  - `NAV_GROUP_ORDER: dict[str, int]`
  - `_flash(message: str | None, kind: str = "info") -> Any`
  - `_locale_switch() -> Any`
  - `_nav(pages: Iterable[AdminPageDefinition]) -> Any`
  - `shell(*, title_text, principal, csrf, theme, color_mode, density, pages, content, flash=None, flash_kind="info", extra_scripts=None, extra_styles=None) -> Any`
  - `error_page(*, title_text: str, message: str, status_hint: int = 403) -> Any`

- [ ] **Step 1: 创建 `views/shell.py`**

  从 `services/admin_web/views_legacy.py` 搬入以下内容（**逐字复制，不要顺手改写**）：

  - 常量 `ADMIN_STYLESHEET`（原 `:62`）、`ADMIN_UI_SCRIPT`（原 `:63`）、`NAV_GROUP_ORDER`（原 `:64`）
  - `_flash`（原 `:73`）
  - `_locale_switch`（原 `:80`）
  - `_nav`（原 `:103`）
  - `shell`（原 `:142`）
  - `error_page`（原 `:2334`）

  新文件头：

  ```python
  """管理端外壳与全局片段。

  ``shell()`` 是所有页面共用的 HTML 外壳（导航、主题、flash 消息）；
  ``_nav`` / ``_locale_switch`` / ``_flash`` 是它的零件。单独成模块是因为
  它们被**每个**页面依赖，放在页面模块里会形成反向依赖。
  """

  from __future__ import annotations

  from collections.abc import Iterable
  from typing import Any

  from ludic.html import (
      a,
      body,
      button,
      div,
      form,
      h1,
      head,
      header,
      html,
      input as input_,
      link,
      main,
      meta,
      nav,
      p,
      script,
      section,
      span,
      title,
  )

  from app.models.admin_control import AdminPageDefinition
  from services.admin_web.accounts import AdminPrincipal
  ```

  **只 import 上面这些**——搬的时候按实际用到的删，别把 `ludic.html` 整包搬过来。

- [ ] **Step 2: 从 `views_legacy.py` 删掉已搬走的内容**

  在 `services/admin_web/views_legacy.py` 里删除上述常量与函数，并加上导入：

  ```python
  from services.admin_web.views.shell import (
      ADMIN_STYLESHEET,
      ADMIN_UI_SCRIPT,
      NAV_GROUP_ORDER,
      _flash,
      _locale_switch,
      _nav,
      error_page,
      shell,
  )
  ```

  注意 `views_legacy.py` 暂时**不能删**——Task 3~6 的页面函数还住在里面，
  它要留到 Task 7 全部搬完才删。

  同时把 `views/__init__.py` 里对应的再导出改成从新模块来：

- [ ] **Step 3: 在 `views/__init__.py` 再导出**

  **是「追加」，不是「替换」。** `views/__init__.py` 现在从 `views_legacy` 再导出 16 个页面函数
  （`error_page` 本轮搬进 `views/shell.py`，所以 `views_legacy` 那侧变成 15 个）。
  这一步要在**保留**页面再导出的同时，**追加** shell 侧的 8 个名字：

  ```python
  from services.admin_web.views.shell import (
      ADMIN_STYLESHEET,
      ADMIN_UI_SCRIPT,
      NAV_GROUP_ORDER,
      _flash,
      _locale_switch,
      _nav,
      error_page,
      shell,
  )
  ```

  > **执行修订（2026-11-09）**：原 Step 3 写的是把 `views_legacy` 的再导出**整块换成**
  > shell 的再导出。若照做，`login_page` / `dashboard` / `workflows_page` 等 16 个页面函数
  > 就不再从 `services.admin_web.views` 导出，`services/admin_web/app.py` 的
  > `from services.admin_web.views import ...` 全部失效——43 个既有后台测试当场全红，
  > 而 `test_views_package_still_exports_every_page` 也会红。
  > **页面再导出必须保留**，两侧合并后包层共导出 24 个名字。

  合并后 `views/__init__.py` 的完整再导出与 `__all__`：

  ```python
  from services.admin_web.views.shell import (
      ADMIN_STYLESHEET,
      ADMIN_UI_SCRIPT,
      NAV_GROUP_ORDER,
      _flash,
      _locale_switch,
      _nav,
      error_page,
      shell,
  )
  from services.admin_web.views_legacy import (
      access_components_page,
      access_pages_page,
      agents_page,
      audit_page,
      dashboard,
      llm_page,
      login_page,
      mappings_page,
      mcp_page,
      oauth_providers_page,
      platform_admins_page,
      preferences_page,
      roles_page,
      skills_page,
      workflow_editor_page,
      workflows_page,
  )

  __all__ = [
      "ADMIN_STYLESHEET",
      "ADMIN_UI_SCRIPT",
      "NAV_GROUP_ORDER",
      "_flash",
      "_locale_switch",
      "_nav",
      "access_components_page",
      "access_pages_page",
      "agents_page",
      "audit_page",
      "dashboard",
      "error_page",
      "llm_page",
      "login_page",
      "mappings_page",
      "mcp_page",
      "oauth_providers_page",
      "platform_admins_page",
      "preferences_page",
      "roles_page",
      "shell",
      "skills_page",
      "workflow_editor_page",
      "workflows_page",
  ]
  ```

  `__all__` 只影响 `from services.admin_web.views import *`，不影响具名导入——
  但它同时是一份「包层承诺了哪些名字」的自述，写全才不会误导后来者。

  > **执行修订（2026-11-09，Task 3 修复轮 1）**：上面的静态再导出块**不能照抄**。
  > 评审查出 `views_legacy` → `views.shell` → 父包 `views/__init__` → `views_legacy` 的循环导入
  > （本任务新引入，隔离基线实测证实基线上不存在），直接
  > `import services.admin_web.views_legacy` 会 ImportError。
  > 控制器选定方案 A：`views/__init__.py` 改用 **PEP 562 模块级 `__getattr__`** 做惰性再导出——
  > **16 个页面函数惰性解析**（首次访问时 `from services.admin_web import views_legacy` 取属性并写回
  > `globals()` 缓存），shell 侧 8 名保持静态导入（不参与环），`__all__` 仍写全 24 名，另配 `__dir__`。
  >
  > **Task 4~7 的 `views/__init__.py` 步骤都按这个结构执行**：
  > - 新模块（`components` / `fields` / `workflows` / `agents` / …）的名字走**静态导入**，照常追加；
  > - 页面函数从 `views_legacy` 搬到新模块时，**同时把该名字从惰性名单里移除**——
  >   留在名单里的名字会通过 `getattr(views_legacy, name)` 解析，而那里已经没有它了；
  > - `__all__` 始终写全「包层承诺的名字」，总数只增不减。
  > - Task 7 删掉 `views_legacy.py` 后，惰性分支连同 `__getattr__` 一起删除，包层回到全静态导入。

- [ ] **Step 4: 运行测试，确认行为未变**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_web.py tests/test_admin_snapshots.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: `test_admin_web` 43 passed；`test_admin_snapshots` **34** passed
  （17 个空数据集页面 + 16 个非空数据集页面 + 1 个路由表，
  **快照逐条相等**——这是「搬过去没改坏」的证据）。

  另外确认 `tests/test_admin_structure.py` 里
  `test_views_package_still_exports_every_page` 仍然**通过**（包层再导出没被搬坏）。

- [ ] **Step 5: 提交**

  ```bash
  git add services/admin_web/views/shell.py services/admin_web/views_legacy.py services/admin_web/views/__init__.py
  git commit -m "refactor(admin): 外壳与全局片段搬进 views/shell.py" -m "shell 被每个页面依赖，放在页面模块里会形成反向依赖，所以单独成模块。内容逐字搬家，渲染快照逐条相等。"
  ```

---

### Task 4: views/components.py —— 通用 UI 组件

**Files:**
- Create: `services/admin_web/views/components.py`
- Modify: `services/admin_web/views_legacy.py`
- Modify: `services/admin_web/views/__init__.py`
- Test: `tests/test_admin_snapshots.py`、`tests/test_admin_structure.py`

**Interfaces:**
- Consumes: `services.admin_web.views.shell.shell`
- Produces（`services.admin_web.views.components`）：
  - `_metric(label_text: str, value: int) -> Any`
  - `_csrf(csrf: str) -> Any`
  - `_toggle_and_delete(*, csrf: str, prefix: str, enabled: bool, can_write: bool) -> Any`
  - `_management_page(*, title_text, lead, principal, csrf, pages, theme, color_mode, density, create_form, headers, rows, flash=None, flash_kind="info") -> Any`

- [ ] **Step 1: 创建 `views/components.py`**

  从 `services/admin_web/views_legacy.py` 搬入（逐字复制）：

  - `_metric`（原 `:486`）
  - `_management_page`（原 `:495`）
  - `_csrf`（原 `:546`）
  - `_toggle_and_delete`（原 `:551`）

  新文件头：

  ```python
  """通用 UI 组件。

  这些是**多个页面共用**的片段：指标卡、CSRF 隐藏域、启停与删除按钮组、
  「标题 + 创建表单 + 数据表」的业务页骨架。抽出来是为了让页面函数只写
  「这个页面有什么」，不重复「每个管理页都长什么样」。
  """

  from __future__ import annotations

  from collections.abc import Iterable
  from typing import Any

  from ludic.html import (
      button,
      div,
      form,
      h1,
      h2,
      input as input_,
      label,
      p,
      section,
      span,
      table,
      tbody,
      td,
      th,
      thead,
      tr,
  )

  from app.models.admin_control import AdminPageDefinition
  from services.admin_web.accounts import AdminPrincipal
  from services.admin_web.views.shell import shell
  ```

- [ ] **Step 2: 从 `views_legacy.py` 删掉已搬走的四个函数并改导入**

  ```python
  from services.admin_web.views.components import (
      _csrf,
      _management_page,
      _metric,
      _toggle_and_delete,
  )
  ```

- [ ] **Step 3: 在 `views/__init__.py` 再导出**

  ```python
  from services.admin_web.views.components import (
      _csrf,
      _management_page,
      _metric,
      _toggle_and_delete,
  )
  ```

  并把这四个名字加进 `__all__`。

- [ ] **Step 4: 运行测试**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_web.py tests/test_admin_snapshots.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 43 passed + 34 passed。

- [ ] **Step 5: 提交**

  ```bash
  git add services/admin_web/views/components.py services/admin_web/views_legacy.py services/admin_web/views/__init__.py
  git commit -m "refactor(admin): 通用 UI 组件搬进 views/components.py" -m "指标卡、CSRF 隐藏域、启停删除按钮组、业务页骨架——四个跨页面复用的片段单独成模块，页面函数只写「这个页面有什么」。"
  ```

---

### Task 5: views/fields.py —— 数据驱动的表单字段

**Files:**
- Create: `services/admin_web/views/fields.py`
- Create: `tests/test_admin_fields.py`
- Modify: `services/admin_web/views/__init__.py`

**Interfaces:**
- Consumes: `ludic.html` 元素构造器
- Produces（`services.admin_web.views.fields`）：
  - `Field` —— 冻结 dataclass，字段：`name: str`、`label: str`、`kind: str = "text"`、
    `required: bool = False`、`options: tuple[tuple[str, str], ...] = ()`、
    `placeholder: str = ""`、`rows: int = 8`、`autocomplete: str = ""`、
    `min_value: str | None = None`、`max_value: str | None = None`、
    `modes: tuple[str, ...] = ("create", "edit")`、
    `edit_placeholder: str = ""`、`edit_rows: int | None = None`
  - `render_field(field: Field, value: object, *, mode: str) -> Any`
  - `render_form(fields: Iterable[Field], *, mode: str, values: Mapping[str, object], csrf: str, action: str, submit_label: str, layout: str = "grid") -> Any`

- [ ] **Step 1: 写失败测试**

  创建 `tests/test_admin_fields.py`：

  ```python
  """数据驱动的表单字段。"""

  from __future__ import annotations

  from ludic.html import div, form, input as input_, label, select, textarea

  from services.admin_web.views.fields import Field, render_field, render_form


  def test_field_renders_text_input() -> None:
      element = render_field(Field("name", "Name", required=True), "abc", mode="edit")
      html = element.to_html()
      assert 'name="name"' in html
      assert 'value="abc"' in html
      assert "required" in html
      assert "Name" in html


  def test_field_renders_password_without_value_leak() -> None:
      element = render_field(
          Field("api_key", "API key", kind="password"), "", mode="edit"
      )
      html = element.to_html()
      assert 'type="password"' in html
      assert 'name="api_key"' in html


  def test_field_renders_textarea_with_rows() -> None:
      element = render_field(
          Field("prompt", "Prompt", kind="textarea", rows=10), "hi", mode="create"
      )
      html = element.to_html()
      assert "<textarea" in html
      assert 'rows="10"' in html


  def test_field_renders_select_with_options() -> None:
      element = render_field(
          Field("enabled", "Enabled", kind="select", options=(("Yes", "1"), ("No", "0"))),
          "1",
          mode="create",
      )
      html = element.to_html()
      assert "<select" in html
      assert 'value="1"' in html
      assert 'value="0"' in html


  def test_field_passes_through_autocomplete_and_bounds() -> None:
      """autocomplete / min / max 必须能表达，否则密码框与数字框无法原样还原。"""
      element = render_field(
          Field(
              "api_key",
              "API key",
              kind="password",
              autocomplete="new-password",
          ),
          None,
          mode="create",
      )
      assert 'autocomplete="new-password"' in element.to_html()

      number = render_field(
          Field("timeout_sec", "Timeout", kind="number", min_value="1", max_value="600"),
          None,
          mode="create",
      ).to_html()
      assert 'type="number"' in number
      assert 'min="1"' in number
      assert 'max="600"' in number


  def test_absent_value_renders_no_value_attribute() -> None:
      """值缺失时**不**渲染 ``value=""``。

      现有表单里 ``input_(name="tools")`` 渲染成 ``<input name="tools">``，
      而不是 ``<input name="tools" value="">``——两者在 HTML 上不等价，
      渲染快照会判为差异。所以「有没有值」必须由调用方显式决定。
      """
      element = render_field(Field("tools", "Tools"), None, mode="create")
      assert "value=" not in element.to_html()

      with_value = render_field(Field("tools", "Tools"), "", mode="edit")
      assert 'value=""' in with_value.to_html()


  def test_edit_mode_uses_edit_placeholder_and_rows() -> None:
      """编辑态用 edit_placeholder / edit_rows，创建态用默认值。"""
      field = Field(
          "api_key",
          "API key",
          kind="password",
          placeholder="创建时的提示",
          edit_placeholder="Leave blank to keep current key",
          rows=8,
          edit_rows=10,
      )
      edit_html = render_field(field, "", mode="edit").to_html()
      create_html = render_field(field, "", mode="create").to_html()
      assert "Leave blank to keep current key" in edit_html
      assert "创建时的提示" in create_html

      textarea_field = Field("prompt", "Prompt", kind="textarea", rows=8, edit_rows=10)
      assert 'rows="10"' in render_field(textarea_field, "", mode="edit").to_html()
      assert 'rows="8"' in render_field(textarea_field, "", mode="create").to_html()


  def test_form_skips_fields_not_in_mode() -> None:
      """modes 不含当前模式的字段不渲染。"""
      fields = [
          Field("code", "Code", required=True, modes=("create",)),
          Field("name", "Name", required=True),
      ]
      create_html = render_form(
          fields, mode="create", values={}, csrf="t", action="/x", submit_label="创建"
      ).to_html()
      edit_html = render_form(
          fields, mode="edit", values={}, csrf="t", action="/x", submit_label="保存"
      ).to_html()
      assert 'name="code"' in create_html
      assert 'name="code"' not in edit_html
      assert 'name="name"' in edit_html


  def test_form_contains_csrf_action_and_submit_label() -> None:
      element = render_form(
          [Field("name", "Name")],
          mode="create",
          values={},
          csrf="token-abc",
          action="/admin/agents",
          submit_label="Create agent",
          layout="grid",
      )
      html = element.to_html()
      assert 'name="csrf_token"' in html
      assert 'value="token-abc"' in html
      assert 'action="/admin/agents"' in html
      assert "Create agent" in html
      assert "form-grid" in html


  def test_form_stack_layout() -> None:
      element = render_form(
          [Field("name", "Name")],
          mode="edit",
          values={},
          csrf="t",
          action="/x",
          submit_label="Save",
          layout="stack",
      )
      assert "stack" in element.to_html()
  ```

- [ ] **Step 2: 运行测试，确认失败**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_fields.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 10 failed（`ModuleNotFoundError: No module named 'services.admin_web.views.fields'`）。

- [ ] **Step 3: 实现 `views/fields.py`**

  创建 `services/admin_web/views/fields.py`：

  ```python
  """数据驱动的表单字段。

  过去同一个实体的「创建表单」与「编辑表单」是**把字段抄两遍**——字段名、
  标签、类型都一样，只有少量差异（创建时多一个 ``code``、编辑时 API key 换
  placeholder、textarea 行数不同）。抄两遍意味着改一个字段要记得改两处，
  漏一处就是前后端字段对不上。

  这里把字段抽成一份 :class:`Field` 描述，创建与编辑共用，差异用
  ``modes`` / ``edit_placeholder`` / ``edit_rows`` 表达。
  """

  from __future__ import annotations

  from collections.abc import Iterable, Mapping
  from dataclasses import dataclass
  from typing import Any

  from ludic.html import (
      button,
      form,
      input as input_,
      label,
      option,
      select,
      span,
      textarea,
  )


  @dataclass(frozen=True)
  class Field:
      """一个表单字段的声明。

      ``modes`` 决定字段出现在哪些表单里（``"create"`` / ``"edit"``）；
      ``edit_placeholder`` / ``edit_rows`` 表达编辑态的差异。

      ``kind`` 覆盖现有表单用到的全部普通控件：``text`` / ``password`` /
      ``email`` / ``tel`` / ``number`` / ``url`` / ``textarea`` / ``select``。
      **不覆盖 radio**——主题选择那种带预览卡片与 checked 逻辑的控件是页面
      特有的布局，保持手写，不硬塞进这套抽象。
      """

      name: str
      label: str
      kind: str = "text"
      required: bool = False
      options: tuple[tuple[str, str], ...] = ()
      placeholder: str = ""
      rows: int = 8
      autocomplete: str = ""
      min_value: str | None = None
      max_value: str | None = None
      modes: tuple[str, ...] = ("create", "edit")
      edit_placeholder: str = ""
      edit_rows: int | None = None


  def _control(field: Field, value: object | None, *, mode: str) -> Any:
      """按 kind 造出控件本身（不含 label 包裹）。

      ``value`` 为 ``None`` 表示「这个输入框不带 value 属性」——现有表单里
      ``input_(name="tools")`` 与 ``input_(name="tools", value="")`` 渲染出的
      HTML 不等价，必须让调用方显式区分。
      """
      text = None if value is None else str(value)
      if field.kind == "textarea":
          rows = field.edit_rows if mode == "edit" and field.edit_rows else field.rows
          return textarea(
              text or "",
              name=field.name,
              rows=rows,
              required=field.required,
          )
      if field.kind == "select":
          return select(
              *(option(label_text, value=option_value) for label_text, option_value in field.options),
              name=field.name,
          )
      placeholder = (
          field.edit_placeholder
          if mode == "edit" and field.edit_placeholder
          else field.placeholder
      )
      extra: dict[str, Any] = {}
      if field.autocomplete:
          extra["autocomplete"] = field.autocomplete
      if field.min_value is not None:
          extra["min"] = field.min_value
      if field.max_value is not None:
          extra["max"] = field.max_value
      return input_(
          name=field.name,
          value=text,
          type="password" if field.kind == "password" else field.kind,
          placeholder=placeholder,
          required=field.required,
          **extra,
      )


  def render_field(field: Field, value: object | None, *, mode: str) -> Any:
      """渲染单个字段（``label`` 包裹控件）。"""
      return label(span(field.label), _control(field, value, mode=mode))


  def render_form(
      fields: Iterable[Field],
      *,
      mode: str,
      values: Mapping[str, object],
      csrf: str,
      action: str,
      submit_label: str,
      layout: str = "grid",
  ) -> Any:
      """渲染整个表单。

      ``values`` 里**显式给出**的键才会带上 ``value`` 属性；没给出的键渲染成
      不带 value 的输入框。这不是细节：``<input name="x">`` 与
      ``<input name="x" value="">`` 在 HTML 上不等价。

      ``layout`` 是约定值：``"grid"``（创建表单，双列栅格）与 ``"stack"``
      （编辑表单，纵向堆叠）。取值只影响外层 class。
      """
      return form(
          input_(type="hidden", name="csrf_token", value=csrf),
          *(
              render_field(field, values.get(field.name), mode=mode)
              for field in fields
              if mode in field.modes
          ),
          button(submit_label, type="submit", class_="btn btn-primary"),
          action=action,
          method="post",
          class_="form-grid" if layout == "grid" else "stack",
      )
  ```

- [ ] **Step 4: 运行测试，确认通过**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_fields.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 10 passed。

- [ ] **Step 5: 在 `views/__init__.py` 再导出**

  ```python
  from services.admin_web.views.fields import Field, render_field, render_form
  ```

  并把三个名字加进 `__all__`。

- [ ] **Step 6: 提交**

  ```bash
  git add services/admin_web/views/fields.py tests/test_admin_fields.py services/admin_web/views/__init__.py
  git commit -m "feat(admin): 数据驱动的表单字段" -m "创建表单与编辑表单共用一份 Field 描述，差异用 modes / edit_placeholder / edit_rows 表达，取代「把字段抄两遍」。本任务只加组件不改页面，接入放在 Task 8。"
  ```

---

### Task 6: views 资源类页面迁移

**Files:**
- Create: `services/admin_web/views/workflows.py`
- Create: `services/admin_web/views/agents.py`
- Create: `services/admin_web/views/skills.py`
- Create: `services/admin_web/views/mcp.py`
- Create: `services/admin_web/views/llm.py`
- Create: `services/admin_web/views/audit.py`
- Modify: `services/admin_web/views_legacy.py`
- Modify: `services/admin_web/views/__init__.py`
- Test: `tests/test_admin_snapshots.py`、`tests/test_admin_web.py`

**Interfaces:**
- Consumes: `services.admin_web.views.shell.shell`、`services.admin_web.views.components.*`
- Produces（各模块导出同名页面函数）：
  - `views/workflows.py`：`workflows_page`、`workflow_editor_page`
  - `views/agents.py`：`agents_page`
  - `views/skills.py`：`skills_page`
  - `views/mcp.py`：`mcp_page`
  - `views/llm.py`：`llm_page`
  - `views/audit.py`：`audit_page`

- [ ] **Step 1: 逐个创建页面模块**

  从 `services/admin_web/views_legacy.py` 搬入（**逐字复制函数体，不改逻辑**）：

  | 新模块 | 搬入的函数（原行号） |
  | --- | --- |
  | `views/workflows.py` | `workflows_page`（`:334`）、`workflow_editor_page`（`:418`） |
  | `views/agents.py` | `agents_page`（`:637`） |
  | `views/skills.py` | `skills_page`（`:750`） |
  | `views/mcp.py` | `mcp_page`（`:865`） |
  | `views/llm.py` | `llm_page`（`:974`） |
  | `views/audit.py` | `audit_page`（`:1075`） |

  每个模块按这个模板写头部（以 `views/agents.py` 为例，其余同构）：

  ```python
  """Agent 配置管理页。"""

  from __future__ import annotations

  from collections.abc import Iterable
  from typing import Any

  from ludic.html import (
      button,
      details,
      div,
      form,
      input as input_,
      label,
      option,
      select,
      small,
      span,
      table,
      td,
      textarea,
      tr,
  )

  from app.models.admin_control import AdminPageDefinition
  from services.admin_web.accounts import AdminPrincipal
  from services.admin_web.views.components import (
      _csrf,
      _management_page,
      _toggle_and_delete,
  )
  ```

  **只 import 实际用到的构造器**。搬完后用
  `.venv\Scripts\python.exe -m pyflakes services\admin_web\views\agents.py` 之类的
  静态检查确认没有未使用的导入（若本机没有 pyflakes，用
  `.venv\Scripts\python.exe -c "import ast,sys;..."` 人工核对亦可）。

  `views/workflows.py` 额外需要外壳（`workflow_editor_page` 自己拼外壳，
  不走 `_management_page`），`views/audit.py` 同样需要外壳。

  > **执行修订（2026-11-09）**：原文写「需要 `views.shell.shell`」是**错的**。
  > 包属性 `services.admin_web.views.shell` 被同名**函数** `shell` 遮蔽
  > （模块导入先置属性，后置的名字绑定覆盖它），所以 `views.shell.shell` 会抛
  > `AttributeError: 'function' object has no attribute 'shell'`。
  > 正确写法是**具名导入**：
  > ```python
  > from services.admin_web.views.shell import shell
  > ```
  > 目标模块是 `sys.modules['services.admin_web.views.shell']`，函数是它的 `shell` 属性。

- [ ] **Step 2: 从 `views_legacy.py` 删掉已搬走的函数**

  删除上述 7 个函数，并在 `views_legacy.py` 顶部加导入：

  ```python
  from services.admin_web.views.agents import agents_page
  from services.admin_web.views.audit import audit_page
  from services.admin_web.views.llm import llm_page
  from services.admin_web.views.mcp import mcp_page
  from services.admin_web.views.skills import skills_page
  from services.admin_web.views.workflows import workflow_editor_page, workflows_page
  ```

- [ ] **Step 3: 在 `views/__init__.py` 再导出**

  把这 7 个名字加进导入与 `__all__`。

- [ ] **Step 4: 运行测试**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_web.py tests/test_admin_snapshots.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 43 passed + 34 passed。**快照必须逐条相等**——任何结构变化都会
  在这里变红。

- [ ] **Step 5: 提交**

  ```bash
  git add services/admin_web/views/ services/admin_web/views_legacy.py
  git commit -m "refactor(admin): 资源类页面按业务域搬进 views 包" -m "workflows / agents / skills / mcp / llm / audit 六个页面各自成模块。内容逐字搬家，渲染快照逐条相等。"
  ```

---

### Task 7: views 其余页面迁移并删除 views_legacy.py

**Files:**
- Create: `services/admin_web/views/login.py`
- Create: `services/admin_web/views/dashboard.py`
- Create: `services/admin_web/views/oauth_providers.py`
- Create: `services/admin_web/views/access.py`
- Create: `services/admin_web/views/platform_admins.py`
- Create: `services/admin_web/views/preferences.py`
- Delete: `services/admin_web/views_legacy.py`
- Modify: `services/admin_web/views/__init__.py`
- Test: `tests/test_admin_structure.py`、`tests/test_admin_snapshots.py`、`tests/test_admin_web.py`

**Interfaces:**
- Consumes: `services.admin_web.views.shell`、`services.admin_web.views.components`
- Produces:
  - `views/login.py`：`login_page`
  - `views/dashboard.py`：`dashboard`
  - `views/oauth_providers.py`：`_provider_form`、`oauth_providers_page`
  - `views/access.py`：`_permission_datalist`、`_permission_editor`、`access_pages_page`、`access_components_page`、`roles_page`、`mappings_page`
  - `views/platform_admins.py`：`platform_admins_page`
  - `views/preferences.py`：`preferences_page`

- [ ] **Step 1: 逐个创建剩余页面模块**

  从 `services/admin_web/views_legacy.py` 搬入（逐字复制）：

  | 新模块 | 搬入的函数（原行号） |
  | --- | --- |
  | `views/login.py` | `login_page` |
  | `views/dashboard.py` | `dashboard` |
  | `views/oauth_providers.py` | `_provider_form`、`oauth_providers_page` |
  | `views/access.py` | `_permission_datalist`、`_permission_editor`、`access_pages_page`、`access_components_page`、`roles_page`、`mappings_page` |
  | `views/platform_admins.py` | `platform_admins_page` |
  | `views/preferences.py` | `preferences_page` |

  > **执行修订（2026-11-09）**：本表原先带「原 `:228` / `:423` / `:1801` …」等行号，
  > 那是**原始 `views.py`** 的行号。Task 3 / Task 4 搬走外壳与组件后，`views_legacy.py`
  > 的行号整体位移；Task 6 又会再搬走 7 个函数，Task 7 开始时的行号已经无法预先写死。
  > **函数名才是准的**——按名字定位，不要按行号定位。
  > Task 6 开始时的实际行号（供交叉参考）：`workflows_page` `:334`、`workflow_editor_page` `:418`、
  > `agents_page` `:637`、`skills_page` `:750`、`mcp_page` `:865`、`llm_page` `:974`、`audit_page` `:1075`。

  每个模块的模块级 docstring 写清这个页面管什么，例如
  `views/access.py`：

  ```python
  """访问控制页：页面权限、组件权限、角色与声明映射。

  这四块放在一个模块里，因为它们共用同一套权限模型（页面定义、组件定义、
  租户覆盖、Casbin 角色），拆开反而要来回 import。
  """
  ```

  `views/access.py` 需要从 `app.models.admin_control` 导入
  `AdminPageDefinition` / `AdminPagePermission` /
  `AdminComponentDefinition` / `AdminComponentPermission` /
  `OAuthRoleMapping` / `SuperAdminAccount`，从 `app.models.admin_user` 导入
  `AdminUser`，从 `app.models.oauth` 导入 `OAuthProvider`——**按实际用到的搬**。

- [ ] **Step 2: 补齐 `views/__init__.py` 的再导出**

  把这 12 个名字（`login_page`、`dashboard`、`_provider_form`、
  `oauth_providers_page`、`_permission_datalist`、`_permission_editor`、
  `access_pages_page`、`access_components_page`、`roles_page`、`mappings_page`、
  `platform_admins_page`、`preferences_page`）加进导入与 `__all__`。

- [ ] **Step 3: 删除过渡模块 `services/admin_web/views_legacy.py`**

  ```bash
  git rm services/admin_web/views_legacy.py
  ```

  **删除前先确认**：`views/__init__.py` 再导出了**全部** 15 个模块的函数，
  且 `services/admin_web/app.py` 的
  `from services.admin_web.views import (...)`（原 `:139`）现在能拿到
  所有名字。少一个就会在 import 时报 `ImportError`。

- [ ] **Step 4: 运行测试**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_web.py tests/test_admin_snapshots.py tests/test_admin_structure.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 43 passed + 34 passed；`test_admin_structure.py` 里
  `test_old_views_module_is_gone` 现在应当 PASS；其余结构测试仍 FAIL
  （`routes/*.py` 还没建、`app.py` 还超长——那是 Task 9 的事）。

- [ ] **Step 5: 提交**

  ```bash
  git add -A services/admin_web/views/ services/admin_web/views_legacy.py
  git commit -m "refactor(admin): 剩余页面迁入 views 包并删除过渡模块 views_legacy.py" -m "旧 views.py 拆成 15 个按业务域切分的模块；views/__init__.py 做再导出，调用方（app.py）不需要改 import 路径。"
  ```

---

### Task 8: fields 组件接入 —— 消除创建/编辑双写

**Files:**
- Modify: `services/admin_web/views/agents.py`
- Modify: `services/admin_web/views/skills.py`
- Modify: `services/admin_web/views/mcp.py`
- Modify: `services/admin_web/views/llm.py`
- Modify: `services/admin_web/views/oauth_providers.py`
- Test: `tests/test_admin_fields.py`、`tests/test_admin_snapshots.py`

**Interfaces:**
- Consumes: `services.admin_web.views.fields.Field` / `render_form`
- Produces: 各资源模块导出字段常量，供页面与后续复用：
  - `views/agents.py`：`AGENT_FIELDS`、`AGENT_CREATE_FIELDS`
  - `views/skills.py`：`SKILL_FIELDS`、`SKILL_CREATE_FIELDS`
  - `views/mcp.py`：`MCP_FIELDS`、`MCP_CREATE_FIELDS`
  - `views/llm.py`：`LLM_FIELDS`、`LLM_CREATE_FIELDS`
  - `views/oauth_providers.py`：`PROVIDER_FIELDS`、`PROVIDER_CREATE_FIELDS`

- [ ] **Step 1: 写失败测试**

  在 `tests/test_admin_fields.py` 末尾追加：

  ```python
  def test_agent_fields_cover_create_and_edit_without_duplication() -> None:
      """Agent 的字段只声明一次，创建与编辑共用。"""
      from services.admin_web.views.agents import AGENT_CREATE_FIELDS, AGENT_FIELDS

      edit_names = {f.name for f in AGENT_FIELDS}
      create_names = {f.name for f in AGENT_CREATE_FIELDS}
      assert edit_names <= create_names
      # code 只在创建时出现
      assert "code" not in edit_names
      assert "code" in create_names
      # api_key 的编辑态提示单独表达，而不是抄一份字段
      api_key = next(f for f in AGENT_FIELDS if f.name == "api_key")
      assert api_key.edit_placeholder == "Leave blank to keep current key"


  def test_agent_create_form_renders_code_but_edit_form_does_not() -> None:
      """接入后的实际渲染结果符合预期。"""
      from services.admin_web.views.agents import AGENT_CREATE_FIELDS, AGENT_FIELDS

      create_html = render_form(
          AGENT_CREATE_FIELDS,
          mode="create",
          values={},
          csrf="t",
          action="/admin/agents",
          submit_label="Create agent",
      ).to_html()
      edit_html = render_form(
          AGENT_FIELDS,
          mode="edit",
          values={},
          csrf="t",
          action="/admin/agents/x/update",
          submit_label="Save changes",
          layout="stack",
      ).to_html()
      assert 'name="code"' in create_html
      assert 'name="code"' not in edit_html
      assert "Create agent" in create_html
      assert "Save changes" in edit_html
  ```

  同理为 skills / mcp / llm / oauth_providers 各写一条「字段只声明一次」的用例，
  模式相同（断言创建字段集包含编辑字段集、且 `code` 不在编辑集里）。

- [ ] **Step 2: 运行测试，确认失败**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_fields.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 新增用例 FAIL（`AGENT_FIELDS` 还不存在）。

- [ ] **Step 3: 把 `agents_page` 改成用字段定义**

  在 `services/admin_web/views/agents.py` 里加：

  ```python
  AGENT_FIELDS = (
      Field("name", "Name", required=True),
      Field("model", "Model", required=True),
      Field("base_url", "Base URL"),
      Field(
          "api_key",
          "API key",
          kind="password",
          autocomplete="new-password",
          edit_placeholder="Leave blank to keep current key",
      ),
      Field("tools", "Tools (comma separated)"),
      Field("skills", "Skills (comma separated)"),
      Field(
          "system_prompt",
          "System prompt",
          kind="textarea",
          required=True,
          rows=8,
          edit_rows=10,
      ),
  )

  AGENT_CREATE_FIELDS = (
      Field("code", "Code", required=True, modes=("create",)),
      *AGENT_FIELDS,
      Field(
          "enabled",
          "Enabled",
          kind="select",
          modes=("create",),
          options=(("Yes", "1"), ("No", "0")),
      ),
  )
  ```

  然后把 `agents_page` 里手写的两段表单换成：

  ```python
      create_form = (
          render_form(
              AGENT_CREATE_FIELDS,
              mode="create",
              values={},
              csrf=csrf,
              action="/admin/agents",
              submit_label="Create agent",
              layout="grid",
          )
          if can_write
          else None
      )
  ```

  编辑表单（原来包在 `details(summary("Edit configuration"), form(...))` 里）换成：

  ```python
              editor = (
                  details(
                      summary("Edit configuration"),
                      render_form(
                          AGENT_FIELDS,
                          mode="edit",
                          values={
                              "name": row.name,
                              "model": row.model,
                              "base_url": row.base_url or "",
                              "tools": ", ".join(row.tools or []),
                              "skills": ", ".join(row.skills or []),
                              "system_prompt": row.system_prompt,
                          },
                          csrf=csrf,
                          action=f"/admin/agents/{row.code}/update",
                          submit_label="Save changes",
                          layout="stack",
                      ),
                  )
                  if can_write
                  else span("")
              )
  ```

  **API key 的编辑态不传值**（`values` 里没有 `api_key`），保持「留空即保留原值」。

- [ ] **Step 4: 对另外四个资源做同样的改造**

  - `skills.py`：`SKILL_FIELDS` / `SKILL_CREATE_FIELDS`，照原表单的字段与标签抄
  - `mcp_page`：`MCP_FIELDS` / `MCP_CREATE_FIELDS`，注意它多一个 `can_refresh` 参数
  - `llm_page`：`LLM_FIELDS` / `LLM_CREATE_FIELDS`
  - `oauth_providers.py`：`_provider_form` 的字段抽成 `PROVIDER_FIELDS` /
    `PROVIDER_CREATE_FIELDS`；注意它有 `create` 参数区分创建与编辑，
    字段集合本身按 `create` 布尔切

  **逐字对照原实现**：标签文案、placeholder、`required`、行数、`autocomplete`、
  按钮文案、表单 layout（创建 `form-grid` / 编辑 `stack`）都必须一致——渲染快照会
  逐条比对，改错一个字都会变红。

  **不纳入 Field 抽象的**：主题选择那种 radio + 预览卡片的控件
  （`preferences_page`）保持手写——它的「checked 随当前主题变」和预览 DOM
  都是页面特有的，硬塞进字段抽象只会把抽象做脏。

- [ ] **Step 5: 运行测试**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_fields.py tests/test_admin_snapshots.py tests/test_admin_web.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: `test_admin_fields` 全通过；**`test_admin_snapshots` 34 passed**
  （说明接入后渲染结构与基线一致）；`test_admin_web` 43 passed。

  如果快照变红，先看差异明细：多半是某处标签文案、placeholder 或行数没抄对，
  修正到与原实现一致即可，**不要**去更新快照——基线是重构前的真实输出。

- [ ] **Step 6: 提交**

  ```bash
  git add services/admin_web/views/ tests/test_admin_fields.py
  git commit -m "refactor(admin): 表单改用字段定义，消除创建/编辑双写" -m "agents / skills / mcp / llm / oauth_providers 的创建表单与编辑表单现在共用一份 Field 描述，差异用 modes / edit_placeholder / edit_rows 表达。渲染快照逐条相等，说明结构未变。"
  ```

---

### Task 9: routes 拆分、common.py 提取与 app.py 瘦身

**Files:**
- Create: `services/admin_web/common.py`
- Create: `services/admin_web/routes/auth.py`
- Create: `services/admin_web/routes/dashboard.py`
- Create: `services/admin_web/routes/workflows.py`
- Create: `services/admin_web/routes/agents.py`
- Create: `services/admin_web/routes/skills.py`
- Create: `services/admin_web/routes/mcp.py`
- Create: `services/admin_web/routes/llm.py`
- Create: `services/admin_web/routes/audit.py`
- Create: `services/admin_web/routes/oauth_providers.py`
- Create: `services/admin_web/routes/access.py`
- Create: `services/admin_web/routes/platform_admins.py`
- Create: `services/admin_web/routes/preferences.py`
- Modify: `services/admin_web/app.py`（从 3373 行降到 ~150 行）
- Test: `tests/test_admin_structure.py`、`tests/test_admin_snapshots.py`、`tests/test_admin_web.py`

**Interfaces:**
- Consumes: `services.admin_web.views`（页面函数）、`services.admin_web.common`（跨域辅助）
- Produces:
  - `services.admin_web.common`：`origin_ok`、`safe_next`、`callback_uri`、`append_query`、
    `principal_of`、`require_csrf`、`tenant_id_of`、`flash_from_query`、`as_bool`、
    `csv_list`、`json_object`、`can_edit_resource`、`operation_authorized`、
    `business_redirect`、`html_error`、`login_redirect`、`unauthorized`、`not_found`
    （**去掉下划线前缀**，因为它们现在是跨模块共享的公开辅助）
  - `services.admin_web.routes.register_all(app)`
  - 每个 `services.admin_web.routes.<域>` 暴露 `register(app)`

- [ ] **Step 1: 先建 `common.py`**

  从 `services/admin_web/app.py` 搬入跨域共用的辅助函数，**去掉下划线前缀**
  （它们现在是跨模块共享的公开 API）：

  | 原函数（原行号） | 新名 |
  | --- | --- |
  | `_origin_ok`（`:261`） | `origin_ok` |
  | `_safe_next`（`:273`） | `safe_next` |
  | `_callback_uri`（`:289`） | `callback_uri` |
  | `_append_query`（`:297`） | `append_query` |
  | `_principal`（`:305`） | `principal_of` |
  | `_preference`（`:332`） | `preference_of` |
  | `_authenticated`（`:352`） | `authenticated` |
  | `_require_csrf`（`:368`） | `require_csrf` |
  | `_login_redirect`（`:383`） | `login_redirect` |
  | `_unauthorized`（`:680`） | `unauthorized` |
  | `_not_found`（`:688`） | `not_found` |
  | `_html_error`（`:697`） | `html_error` |
  | `_tenant_id`（`:716`） | `tenant_id_of` |
  | `_flash_from_query`（`:721`） | `flash_from_query` |
  | `_as_bool`（`:746`） | `as_bool` |
  | `_csv`（`:753`） | `csv_list` |
  | `_json_object`（`:762`） | `json_object` |
  | `_can_edit_resource`（`:770`） | `can_edit_resource` |
  | `_operation_authorized`（`:783`） | `operation_authorized` |
  | `_business_redirect`（`:805`） | `business_redirect` |

  **只搬跨域共用的**：只被一个域用到的辅助（如 `_agent_form_values`、
  `_provider_form_values`、`_parse_permissions`、`_may_bind_permissions`、
  `_target_binding_tenant`、`_strip_value_error_prefix`、`_pydantic_field_path`、
  `_pydantic_message`、`_definition_errors`、`_validate_mcp_connection`）
  **留在 `app.py` 或搬到对应域模块**，不要一股脑塞进 `common.py`。

  模块头：

  ```python
  """跨业务域共用的请求处理辅助。

  这些函数原先是 ``app.py`` 里的私有函数，拆包后被多个路由模块用到，
  因此提升为公开 API 并集中到一处——集中在这里才能避免每个路由模块都
  从 ``app.py`` 导入（那会造成循环导入）。
  """
  ```

- [ ] **Step 2: 写失败测试**

  在 `tests/test_admin_structure.py` 末尾追加：

  ```python
  def test_common_module_exposes_shared_helpers() -> None:
      """跨域辅助集中在 common.py，且不再带下划线前缀。"""
      module = __import__("services.admin_web.common", fromlist=["origin_ok"])
      for name in (
          "origin_ok",
          "safe_next",
          "callback_uri",
          "append_query",
          "principal_of",
          "require_csrf",
          "tenant_id_of",
          "flash_from_query",
          "as_bool",
          "csv_list",
          "json_object",
          "can_edit_resource",
          "operation_authorized",
          "business_redirect",
      ):
          assert callable(getattr(module, name, None)), name
  ```

- [ ] **Step 3: 运行测试，确认失败**

  Expected: FAIL（`common.py` 不存在）。

- [ ] **Step 4: 建 12 个路由模块**

  每个模块的形状一致（以 `routes/agents.py` 为例）：

  ```python
  """Agent 的管理路由。"""

  from __future__ import annotations

  from typing import Any

  from ludic.web import Request

  from services.admin_web.common import (
      as_bool,
      business_redirect,
      can_edit_resource,
      csv_list,
      flash_from_query,
      json_object,
      operation_authorized,
      principal_of,
      require_csrf,
      tenant_id_of,
  )
  from services.admin_web.views import agents_page


  def register(app: Any) -> None:
      """把 Agent 的路由挂到 app 上。"""

      @app.get("/admin/agents")
      async def agents_get(request: Request) -> Any:
          ...

      @app.post("/admin/agents")
      async def agents_post(request: Request) -> Any:
          ...
  ```

  **关键要求**：
  1. 路由函数的**路径、方法、名字**必须与原 `app.py` 逐字一致；
  2. 路由注册顺序必须与原 `app.py` 的装饰器顺序一致——`routes/__init__.py`
     的 `register_all` 已经把顺序固定为
     `(auth, dashboard, workflows, agents, skills, mcp, llm, audit,
     oauth_providers, access, platform_admins, preferences)`；
  3. 域模块**不 import `services.admin_web.app`**（避免循环导入）；
  4. 路由内部调用 `services.admin_web.common` 里的辅助，**不再从 `app.py` 导入**。

  各域的路径清单（照抄自现有 `app.py` 的装饰器）：

  | 域模块 | 路径 |
  | --- | --- |
  | `auth` | `GET /admin/health`、`GET /admin/login`、`POST /admin/login/super-admin`、`POST /admin/login/oauth/{provider_code}`、`GET /admin/auth/oauth/{provider_code}/callback`、`POST /admin/logout` |
  | `dashboard` | `GET /admin/` |
  | `workflows` | `GET /admin/workflows`、`GET /admin/workflows/{code}`、`POST /admin/workflows/validate`、`POST /admin/workflows`、`POST /admin/workflows/{code}/update`、`POST /admin/workflows/{code}/toggle`、`POST /admin/workflows/{code}/delete` |
  | `agents` | `GET/POST /admin/agents`、`POST /admin/agents/{code}/update`、`POST /admin/agents/{code}/toggle`、`POST /admin/agents/{code}/delete` |
  | `skills` | `GET/POST /admin/skills`、`POST /admin/skills/{code}/update`、`POST /admin/skills/{code}/archive` |
  | `mcp` | `GET/POST /admin/mcp`、`POST /admin/mcp/{code}/update`、`POST /admin/mcp/{code}/toggle`、`POST /admin/mcp/{code}/delete`、`POST /admin/mcp/{code}/refresh` |
  | `llm` | `GET/POST /admin/llm`、`POST /admin/llm/{code}/update`、`POST /admin/llm/{code}/toggle`、`POST /admin/llm/{code}/delete` |
  | `audit` | `GET /admin/audit` |
  | `oauth_providers` | `GET/POST /admin/oauth-providers`、`POST /admin/oauth-providers/{code}/update`、`POST /admin/oauth-providers/{code}/toggle`、`POST /admin/oauth-providers/{code}/delete` |
  | `access` | `GET /admin/access/pages`、`POST /admin/access/pages/{page_code}/permissions`、`GET /admin/access/components`、`POST /admin/access/components/{component_code}/permissions`、`GET /admin/access/roles`、`GET/POST /admin/access/mappings` |
  | `platform_admins` | `GET/POST /admin/platform-admins`、`POST /admin/platform-admins/{account_id}/delete`、`POST /admin/super-admin/forgot-password`、`POST /admin/super-admin/reset-my-password` |
  | `preferences` | `GET/POST /admin/preferences` |

  只被单域使用的私有辅助跟着搬到该域模块：`_agent_form_values` → `routes/agents.py`，
  `_provider_form_values` → `routes/oauth_providers.py`，
  `_parse_permissions` / `_may_bind_permissions` / `_target_binding_tenant` →
  `routes/access.py`，`_strip_value_error_prefix` / `_pydantic_field_path` /
  `_pydantic_message` / `_definition_errors` → `routes/workflows.py`，
  `_validate_mcp_connection` → `routes/mcp.py`。

- [ ] **Step 5: 瘦身 `app.py`**

  `services/admin_web/app.py` 最终只保留：

  ```python
  """管理端应用装配。

  路由与页面已按业务域拆到 ``services.admin_web.routes`` 与
  ``services.admin_web.views`` 两个包里；这里只负责把应用组装起来：
  创建 app、装生命周期、加中间件、挂静态资源、注册路由。
  """

  from __future__ import annotations

  import asyncio
  import logging
  from contextlib import asynccontextmanager
  from pathlib import Path

  from ludic.web import LudicApp
  from starlette.middleware.base import BaseHTTPMiddleware
  from starlette.staticfiles import StaticFiles

  from services.admin_web.db import AdminSessionLocal, dispose_admin_engine
  from services.admin_web.registry import seed_registry
  from services.admin_web.rbac import casbin_service
  from services.admin_web.accounts import ensure_owner_super_admin, rotate_expired_super_admins

  logger = logging.getLogger(__name__)
  STATIC_DIR = Path(__file__).with_name("static")


  async def _rotation_loop() -> None:
      """每小时执行一次 30 天超级管理员密码轮换。"""
      ...


  @asynccontextmanager
  async def lifespan(_: LudicApp):
      """初始化 owner、registry/Casbin，并启动密码轮换任务。"""
      ...


  app = LudicApp(lifespan=lifespan)
  app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin-static")


  async def security_headers(request, call_next):
      """添加后台安全响应头。"""
      ...


  app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)

  from services.admin_web import routes

  routes.register_all(app)
  ```

  `lifespan` 与 `security_headers` 的**函数体逐字保留**（它们不在本次拆分范围
  内，只是搬家）。

- [ ] **Step 6: 运行测试**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_admin_web.py tests/test_admin_snapshots.py tests/test_admin_structure.py -q -p pytest_asyncio.plugin -p pytest_timeout
  ```

  Expected: 43 passed + 34 passed + 结构测试全绿（路由表快照相等证明
  路径、方法与**顺序**都没变）。

- [ ] **Step 7: 全量回归**

  Run:
  ```powershell
  $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
  .venv\Scripts\python.exe -m pytest tests/test_workflow_def.py tests/test_api_e2e.py tests/test_mysql_schema_compat.py tests/test_admin_entrypoint.py -q -p pytest_asyncio.plugin -p pytest_timeout
  node --test tests/js/workflow-editor-core.test.mjs
  node --test tests/js/workflow-editor.test.mjs
  ```

  Expected: 39 + 15 + 3 + 12 passed；JS 22/22、52/52。

  已知环境问题（**不是**本次缺陷）：`tests/test_mcp_e2e.py` 会挂死；
  `tests/test_studio_service.py::test_request_id_dedup_skips_second_run` 失败；
  `test_postgres_compat.py` / `test_pg_pitr.py` 需要外部 PostgreSQL。
  跑全量会在仓库根瞬时产生 0 字节 `tmp*` 文件（套件自清）——**判定工作树干净
  必须在套件结束后**。

- [ ] **Step 8: 检查文件行数**

  Run:
  ```powershell
  Get-ChildItem services\admin_web -Recurse -Filter *.py |
    Where-Object { $_.FullName -notmatch '__pycache__' } |
    ForEach-Object { "{0,6}  {1}" -f (Get-Content $_.FullName | Measure-Object -Line).Lines, $_.FullName.Replace((Get-Location).Path + '\','') } |
    Sort-Object -Descending
  ```

  Expected: `app.py` ≤ 200 行；`common.py` ≤ 300 行；其余模块 ≤ 350 行。

- [ ] **Step 9: 提交**

  ```bash
  git add services/admin_web/ tests/test_admin_structure.py
  git commit -m "refactor(admin): 路由按业务域拆包，app.py 只剩装配" -m "12 个路由模块各暴露 register(app)，装饰器写法与路径、方法、注册顺序全部不变（路由表快照保序比对）。跨域共用的请求辅助提取到 common.py 并去掉下划线前缀；只被单域用的辅助留在各自域模块。"
  ```

---

### Task 10: 真机验证与文档回写

**Files:**
- Modify: `services/admin_web/README.md`
- Modify: `app/design/v4.8-workflow-visual-editor.md`（仅在需要说明目录结构时）
- Test: 目标机上的已部署环境

**Interfaces:**
- Consumes: 192.168.5.18 上已部署的 `webagent-admin` 容器
- Produces: 部署侧记录与文档口径

- [ ] **Step 1: 重建并重启管理端镜像**

  Run（在目标机 `192.168.5.18` 上）：
  ```bash
  cd /docker/web-agent-stack
  PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    docker compose -f deploy/docker-compose.stack.yml --env-file .env build admin-web
  docker compose -f deploy/docker-compose.stack.yml --env-file .env up -d admin-web
  ```

  Expected: 镜像构建成功，容器 `webagent-admin` 变为 healthy。

- [ ] **Step 2: 跑部署侧验证脚本**

  Run:
  ```powershell
  .venv\Scripts\python.exe .superpowers\sdd\2026-11-08-workflow-visual-editor\_deploy\verify_admin.py
  ```

  Expected: 登录 → 建 Agent → 建工作流 → 编排页 200 → 三个静态资源 200 →
  `POST /admin/workflows/validate` 对成环定义返回
  `nodes=["a","b","c"]`、`edges=["a->b","b->c","c->a"]`。

- [ ] **Step 3: 跑真实浏览器验证**

  Run:
  ```powershell
  node .superpowers\sdd\2026-11-08-workflow-visual-editor\_deploy\live_editor.mjs
  ```

  Expected: **14/14 通过** —— 登录、编辑器初始化、画布尺寸、节点渲染、
  标题文本、样式生效、拖拽写回模型、撤销可用、真实端点返回环与整组定位、
  全程无前端异常。

- [ ] **Step 4: 跑 OAuth 全流程验证**

  Run:
  ```powershell
  node .superpowers\sdd\2026-11-08-workflow-visual-editor\_deploy\oauth_login.mjs
  ```

  Expected: **10/10 通过**。

- [ ] **Step 5: 更新文档**

  在 `services/admin_web/README.md` 里补一段「代码结构」，写清：

  ```markdown
  ## 代码结构

  ```
  services/admin_web/
    app.py          应用装配：创建 app、lifespan、中间件、静态挂载、注册路由
    common.py       跨业务域共用的请求处理辅助
    routes/         各业务域的路由模块，每个暴露 register(app)
    views/          各业务域的页面模块 + shell / components / fields
  ```

  路由注册顺序固化在 `routes/__init__.py` 的 `register_all` 里——Starlette 首个
  匹配 path 的路由胜出，顺序是行为的一部分。页面渲染的回归保护是
  `tests/snapshots/admin/` 下的元素树快照。
  ```

  同时在 `docs/design/README.md` 的版本台账里，V4.8 行补一句「管理端后端按业务域
  拆成 routes/ 与 views/ 两个包」。

- [ ] **Step 6: 提交并推送**

  ```bash
  git add services/admin_web/README.md app/design/v4.8-workflow-visual-editor.md docs/design/README.md
  git commit -m "docs: 管理端代码结构说明" -m "写清 routes/ 与 views/ 两个包的职责、路由注册顺序为何重要，以及元素树快照在哪。"
  git push
  ```

  > 推送时本机 OpenSSH 与 GitHub 的 KEX 协商会失败（服务器首选
  > `sntrup761x25519-sha512`，客户端不支持），用
  > `git -c core.sshCommand="ssh -o KexAlgorithms=curve25519-sha256" push`。

---

## Self-Review

**1. 设计文档覆盖检查**

| 设计文档章节 | 对应 Task |
| --- | --- |
| §4.1 `routes/` 十二个域模块 + `register_all` | Task 2（骨架）、Task 9（实现） |
| §4.2 `views/` 十五个模块 | Task 2（骨架）、Task 3–7 |
| §4.3 `common.py` 提取 | Task 9 |
| §5 数据驱动表单字段 | Task 5（组件）、Task 8（接入） |
| §6 元素树快照 + 路由表快照 | Task 1 |
| §7 四个批次 | 展开为 Task 1–10 |
| §8 风险与缓解 | 分散在各 Task 的验证步骤 |
| §9 不碰既有约束 | Global Constraints |

**2. 占位符扫描**：本计划无 TBD / TODO / 「类似 Task N」/ 无代码的代码步骤。

**3. 类型一致性**

- `Field` 的字段名在 Task 5 定义、Task 8 使用，一致；
- `Field` 的属性集覆盖了现有表单用到的全部普通控件属性
  （`kind` / `required` / `options` / `placeholder` / `rows` / `autocomplete` /
  `min_value` / `max_value` / `modes` / `edit_placeholder` / `edit_rows`）；
- `render_form` 的 `values` 语义在 Task 5 定义为「**显式给出才带 value 属性**」，
  Task 8 的调用点（创建 `values={}`、编辑 `values={...}`）与之匹配；
- `render_form` 的签名在 Task 5 定义、Task 8 调用，一致；
- `normalize` / `route_table` / `write_snapshot` / `read_snapshot` 在 Task 1 定义，
  Task 1 的测试里使用，一致；
- `register(app)` 在 Task 2 约定，Task 9 实现，一致；
- `common.py` 的 15 个公开名在 Task 9 Step 1 的表里定义，Step 2 的测试只断言其中
  14 个（`preference_of` / `authenticated` / `login_redirect` / `unauthorized` /
  `not_found` / `html_error` 未在断言列表里——它们是内部使用的，
  保留在表里但不强制断言）。
