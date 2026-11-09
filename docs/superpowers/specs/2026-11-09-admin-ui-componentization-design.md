# 管理端 UI 组件化与模块拆分设计

**日期：** 2026-11-09
**范围：** `services/admin_web/` 下的 `app.py`（3373 行）与 `views.py`（2303 行）
**状态：** 已确认，待实现

## 1. 背景与问题

管理端（V4.7 起独立成 `admin-web` 容器）的后端代码集中在两个文件里：

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `services/admin_web/app.py` | 3373 | 约 50 个路由 + 业务逻辑 + 生命周期 + 中间件 |
| `services/admin_web/views.py` | 2303 | 15 个页面渲染函数 + 5 个片段 + 外壳 |

带来的问题：

1. **单文件过长**：两文件合计 5676 行，任何改动都要在几百行的文件里定位上下文；
2. **重复样板**：视图层有 88 处手写表单字段行，其中同一实体的「创建表单」与
   「编辑表单」各写一遍——`agents_page` 16 行（8 个字段写两遍）、
   `_provider_form` 20 行、`mcp_page` 10 行、`llm_page` 9 行、`skills_page` 8 行；
3. **没有渲染回归保护**：现有 37 个测试的断言粒度是「页面可访问 / 含某元素」，
   重构后即使 HTML 面目全非也测不出来。

## 2. 已核实的事实（2026-11-09 核对）

这些是设计的事实基础，实现时不得与之冲突：

- `LudicApp` 是 **Starlette** 子类（`starlette.applications.Starlette`），**不是**
  FastAPI —— 没有 `APIRouter`，路由用 Starlette 的 `@app.get` 装饰器或
  `starlette.routing.Route`；
- `views.py` 的 15 个页面函数被 `app.py` 用 `from services.admin_web.views import (...)`
  集中导入；
- `app.py` 里 `app = LudicApp(lifespan=lifespan)`（`:238`）、
  `app.mount("/admin/static", StaticFiles(...))`（`:239`）、
  `app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)`（`:258`）；
- `services/admin_web/` 下 `accounts.py`(351) / `security.py`(216) /
  `notifications.py`(151) / `oauth_admin.py`(115) / `audit.py`(89) /
  `db.py`(50) / `rbac.py`(17) / `registry.py`(550) 已是独立模块；
- `admin.css` 1932 行、**371 个 class 选择器**；
- 本机**没有任何 HTML 解析库**（bs4 / lxml / html5lib / selectolax 均不可用），
  且不允许为此引入新依赖；
- Ludic 元素对象结构：`.attrs` 是 Python 属性名（`class_`），
  `.aliased_attrs` 是 HTML 属性名（`class`），`.html_name` 是标签名，
  `.children` 是元素与字符串的混排元组；
- Starlette 路由**顺序敏感**（首个匹配 path 的路由胜出）。

## 3. 目标与非目标

### 目标

1. `app.py` 降到约 150 行，只保留应用装配；
2. 视图与路由按业务域拆成独立模块，**任何单文件不超过 350 行**；
3. 表单字段改为数据驱动，创建与编辑共用一份字段定义，消除双写；
4. 建立渲染回归保护：重构前后页面渲染结果**结构等价**；
5. 路由注册顺序成为可测事实，而不是靠人记。

### 非目标

1. **不改 class 名**。371 个 CSS 选择器牵连下，重命名的收益远小于样式回归风险；
   保持 class 不变本身就满足「结构等价」的要求；
2. 不改任何业务逻辑、错误文案、权限判定、CSRF 与 Origin 校验链路；
3. 不引入第三方依赖、不加前端构建链、不碰 `admin.css`；
4. 不改 `admin.py` 之外的客户端代码。

## 4. 目标架构

```
services/admin_web/
  app.py           ~150 行   app 创建 / lifespan / 安全响应头中间件 / 静态挂载 / register_all
  common.py        ~250 行   跨域工具（原 app.py 内的私有辅助函数）
  routes/          12 个域模块 + __init__.py
  views/           16 个模块 + __init__.py
  accounts.py security.py notifications.py oauth_admin.py audit.py db.py rbac.py registry.py
                   ↑ 现有模块，本次不动
```

### 4.1 `routes/`

按业务域切分，每个模块暴露 `register(app)`：

| 模块 | 承载路由 |
| --- | --- |
| `auth.py` | `/admin/health`、`/admin/login`、`/admin/login/super-admin`、`/admin/login/oauth/{code}`、`/admin/auth/oauth/{code}/callback`、`/admin/logout` |
| `dashboard.py` | `/admin/`（登录后的控制台概览） |
| `workflows.py` | `/admin/workflows*`（含 `validate`） |
| `agents.py` / `skills.py` / `mcp.py` / `llm.py` | 各资源的增删改查与启停 |
| `audit.py` | `/admin/audit` |
| `oauth_providers.py` | `/admin/oauth-providers*` |
| `access.py` | `/admin/access/pages`、`components`、`roles`、`mappings` |
| `platform_admins.py` | `/admin/platform-admins*`、`/admin/super-admin/forgot-password`、`reset-my-password` |
| `preferences.py` | `/admin/preferences*` |

`routes/__init__.py` 固化注册顺序：

```python
def register_all(app) -> None:
    """按重构前的装饰器顺序注册各域路由。

    顺序是**行为的一部分**：Starlette 首个匹配 path 的路由胜出。
    """
    for module in (auth, dashboard, workflows, agents, skills, mcp, llm, audit,
                   oauth_providers, access, platform_admins, preferences):
        module.register(app)
```

域模块**不 import app 对象**（`app` 由参数传入），避免循环依赖。
`@app.get(...)` / `@app.post(...)` 的写法保持不变，只是整体搬家。

### 4.2 `views/`

| 模块 | 内容 |
| --- | --- |
| `shell.py` | `shell()`、`_flash()`、`_nav()`、`_locale_switch()`、`error_page()` |
| `components.py` | `_metric()`、`_csrf()`、`_toggle_and_delete()`、`_management_page()`、数据表格 |
| `fields.py` | `Field` 描述与表单渲染（见 §5） |
| `login.py` / `dashboard.py` | 登录页、概览页 |
| `workflows.py` / `agents.py` / `skills.py` / `mcp.py` / `llm.py` / `audit.py` | 各业务页面 |
| `oauth_providers.py` / `access.py` / `roles.py` / `platform_admins.py` / `preferences.py` | 其余页面 |

> `access.py` 与 `roles.py` 是拆开的：页面权限 + 组件权限放 `access.py`（共用同一套
> 权限模型），租户角色与 OAuth 声明映射放 `roles.py`。两者合在一个模块会有
> **395 行**，超过「任何单文件不超过 350 行」的上限。

`views/__init__.py` 只做再导出，保持 `from services.admin_web.views import agents_page`
这类调用点可用（内部路径调整后统一从这里出）。

### 4.3 `common.py`

收拢当前 `app.py` 里被多个域共用的私有辅助：`_origin_ok`、`_safe_next`、
`_callback_uri`、`_append_query`、`_principal`、`_require_csrf`、`_tenant_id`、
`_flash_from_query`、`_as_bool`、`_csv`、`_json_object`、`_can_edit_resource`、
`_operation_authorized`、`_business_redirect`、`_html_error`、`_login_redirect`、
`_unauthorized`、`_not_found`。

**只放跨域共用的**；只被一个域用的辅助留在该域模块里。

## 5. 组件抽象：数据驱动的表单字段

### 5.1 问题

同一实体的「创建表单」与「编辑表单」字段完全相同，只是：创建时 `code` 必填、
编辑时 `code` 不出现；API key 在编辑态换 placeholder；编辑态的 textarea 行数不同；
按钮文案与表单 layout 不同。现在这些差异靠**把字段抄两遍**来表达。

### 5.2 字段描述

```python
@dataclass(frozen=True)
class Field:
    """一个表单字段的声明。

    ``modes`` 决定字段出现在哪些表单里（``"create"`` / ``"edit"``），
    ``edit_placeholder`` / ``edit_rows`` 表达编辑态的差异——这些差异过去
    是靠把字段抄两遍来表达的。
    """

    name: str
    label: str
    kind: str = "text"           # text | password | textarea | select | number
    required: bool = False
    options: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""
    rows: int = 8
    modes: tuple[str, ...] = ("create", "edit")
    edit_placeholder: str = ""
    edit_rows: int | None = None
```

### 5.3 渲染

```python
def render_field(field: Field, value: object, *, mode: str) -> Any:
    """渲染单个字段；``mode`` 决定取哪个变体。"""

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

    ``layout`` 是约定值：``"grid"``（创建表单，双列栅格）与 ``"stack"``
    （编辑表单，纵向堆叠）。取值只影响外层 class。
    """```

### 5.4 使用

```python
AGENT_FIELDS = (
    Field("name", "Name", required=True),
    Field("model", "Model", required=True),
    Field("base_url", "Base URL"),
    Field("api_key", "API key", kind="password",
          edit_placeholder="Leave blank to keep current key"),
    Field("tools", "Tools (comma separated)"),
    Field("skills", "Skills (comma separated)"),
    Field("system_prompt", "System prompt", kind="textarea",
          required=True, rows=8, edit_rows=10),
)
```

创建表单 = `AGENT_FIELDS` + 仅创建态的字段（`code`、`enabled`）；
编辑表单 = `AGENT_FIELDS`（自动套用 `edit_placeholder` / `edit_rows`）。

预计消除的重复：agents 16→8 行、skills 8→4、mcp 10→5、llm 9→5、
oauth_providers 20→10；`_provider_form` 从 215 行降到约 120 行。

### 5.5 边界

组件化只覆盖**表单与列表**这两类样板。页面特有的布局（如编排页的三栏、
登录页的滑动面板）保持各页自绘，不强行抽象。

## 6. 验证机制

### 6.1 元素树快照

**不解析 HTML，直接比对 Python 元素树**——Ludic 渲染出的就是元素对象，
在 Python 层比对比解析 HTML 字符串精确，且零新依赖。

```python
def normalize(element) -> dict:
    """元素 → 可比对 JSON。

    - 标签名取 ``html_name``；
    - 属性取 ``aliased_attrs``（HTML 名），并排序——属性顺序变化不算差异；
    - 文本与子节点分别递归（``children`` 是元素与字符串的混排元组）。
    """
```

**归一化占位符**（否则每次渲染都不同，快照必然失败）：

| 内容 | 归一化为 |
| --- | --- |
| `csrf_token` 属性值 | `<CSRF>` |
| 文本中的 ISO 时间戳 | `<TS>` |

### 6.2 两份快照

| 文件 | 内容 |
| --- | --- |
| `tests/snapshots/admin/routes.json` | 路由表：`method` + `path` + **顺序** |
| `tests/snapshots/admin/pages/*.json` | 各页面渲染结果的元素树 |

**路由表快照是强保证**：拆分前后路由序列逐条相同，把「注册顺序」从口头约定
变成可测事实。页面快照保证渲染结构不变。

比对按「结构等价」：属性顺序变化不计差异，标签结构与文本变化计差异。

**class 名变化会被判为差异**——这是 §3「不改 class 名」的技术兜底：快照里记着
每个 class 的值，一旦有人重命名，快照立刻变红，而不是等到线上掉样式才发现。

### 6.3 回归命令

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
.venv\Scripts\python.exe -m pytest tests/test_admin_web.py tests/test_admin_snapshots.py -q -p pytest_asyncio.plugin -p pytest_timeout
node --test tests/js/workflow-editor-core.test.mjs
node --test tests/js/workflow-editor.test.mjs
```

## 7. 批次划分

每批一个提交，每批跑全量 pytest 与两个 JS 套件。

| 批次 | 内容 | 验证 |
| --- | --- | --- |
| 1 | 快照基建 + 抓基线（**不动生产代码**） | 快照文件生成成功 |
| 2 | `views/` 拆分 + `components.py` / `fields.py` 抽象 | 页面快照逐条相等 |
| 3 | `routes/` 拆分 + `app.py` 瘦身 | 路由表快照相等 |
| 4 | `common.py` 提取 + 删除旧 `views.py` | 全量回归 + 真机验证 |

批次 1 必须在最前：没有基线快照，后面每一批的「没改坏」都只是断言。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 路由注册顺序变化导致 404/405 行为漂移 | 路由表快照（含顺序）逐条比对 |
| 组件抽象改变渲染结构 | 元素树快照；属性排序归一化 |
| 拆包引入循环导入 | 域模块不 import app 对象；`views/__init__.py` 统一再导出 |
| `common.py` 变成新的垃圾桶 | 只收跨域共用的；单域辅助留在域模块 |
| 快照夹具依赖真实数据库 | 复用现有 `_create_seeded_logged_in_super_admin()` 夹具 |
| 快照因随机值失效 | 归一化占位符（CSRF / 时间戳） |

## 9. 与既有约束的关系

- 仓库约定：docstring 与注释一律中文；新增模块带模块级说明；
- 不新增数据库迁移，不改客户端 API，不改前端资源；
- 本设计不触碰 `app/core/workflow_def.py`（图规则唯一实现）与 V4.8 的编排页改动；
- 部署侧无需改动：镜像只 COPY `services/admin_web/`，拆包后目录结构仍在
  `services/admin_web/` 下，`Dockerfile.admin` 的 `COPY services/admin_web/ ./services/admin_web/`
  无需调整。
