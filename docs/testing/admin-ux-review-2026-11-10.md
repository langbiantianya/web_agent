# 管理后台交互与设计问题记录（2026-11-10）

本轮只记录问题，安排后续统一优化。严重度：

- **P0**：可能造成数据错误、安全误操作或无法完成任务。
- **P1**：明显增加操作成本或容易误判。
- **P2**：可用性、可访问性或视觉一致性问题。

## 问题清单

| ID | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| UX-01 | P0 | 多数资源列表 | “删除”是单击立即提交，没有二次确认、影响摘要或可恢复入口 | 使用明确对象名的确认对话框；优先软删除，或至少提供短时撤销 |
| UX-02 | P0 | `/admin/agents/{code}/delete` | 删除 Agent 未检查组件的被工作流引用情况，可能让现有工作流运行失败 | 删除前列出引用工作流并阻止，或仅允许停用 |
| UX-03 | P1 | 所有创建/编辑表单 | 校验失败只重定向为通用的 “The submitted values are invalid.”，已填内容丢失，也不定位字段 | 原页面保留输入并显示字段级错误；无法保留时至少用 Flash 写明具体字段 |
| UX-04 | P1 | `/admin/agents` | 连接下拉包含停用项，但选择后才由服务端给出泛化错误；模型留空时也不会即时展示将采用的默认模型 | 禁用停用 option；选择连接后即时预填/提示默认模型 |
| UX-05 | P1 | `/admin/llm` | 被 Agent 使用的连接仍显示可点击的停用/删除按钮，点击后才提示“in use” | 在行内显示引用数量，并禁用/解释受限操作 |
| UX-06 | P1 | 所有管理列表 | 除审计外普遍没有搜索、筛选、排序和分页；接口一次最多展示 500 条 | 统一增加搜索、空态/加载态、分页与 URL 可分享筛选 |
| UX-07 | P1 | LLM 探测 | “获取模型/测试连接”只有文字状态，没有 `aria-live`、忙碌进度或结果详情入口 | 增加进度、可访问状态区、耗时和错误排查链接 |
| UX-08 | P1 | Agent 旧配置 | 未绑定 LLM 的存量 Agent 只显示 “Legacy / manual”，无法直接看到其旧 Base URL，也没有迁移助手 | 显示脱敏旧地址与“迁移到连接”引导；密钥仍保持只写 |
| UX-09 | P1 | 所有可编辑列表 | 行内 `<details>` 展开编辑，表单可长达整屏；多个行可同时展开，缺少主编辑面板或深链接 | 使用独立编辑抽屉/页面；支持 `?resource=code` 深链接和未保存离开保护 |
| UX-10 | P2 | 全局中文化 | 页面先服务端输出英文，再由 DOM 文本替换为中文，刷新时可见英文闪现；动态插入内容容易漏翻 | 在服务端按 Locale 渲染，或至少首屏隐藏后再切换 |
| UX-11 | P2 | 列表与提示 | “Configured resources”“Create resource”等重复标题占据较多纵向空间，资源多时列表被推到下方 | 缩短页头，把创建动作收进主按钮/抽屉，列表优先显示 |
| UX-12 | P2 | 表格窄屏 | 表格只依赖横向滚动；操作列往往在很右侧，窄屏手势成本高 | 720px 以下改成卡片行，主操作固定或折叠到菜单 |
| UX-13 | P2 | 启停状态 | 状态只以文字 “Enabled/Disabled” 呈现，无明显颜色或图标语义 | 增加带文字的语义徽标，避免仅靠颜色 |
| UX-14 | P2 | 登录页 | OAuth 是主视觉，超级管理员登录入口是次级文字；无 OAuth 提供方时主区域可能显得不可用 | 无 IdP 时直接把密码登录提升为主操作；有 IdP 时保留次级入口 |
| UX-15 | P2 | 审计/管理员时间字段 | 时间格式可能直接显示原始时间值，缺少时区、相对时间和排序提示 | 统一格式化并显示时区；允许按时间排序 |
| UX-16 | P2 | 可访问性 | 行内表单与详情按钮缺少统一的上下文名称，读屏可能只听到“删除”“启用” | 使用对象名补齐可访问名称，如“删除 Agent e2e-order” |
| UX-17 | P2 | 全局操作反馈 | Flash 提示为页面顶部短文本，没有可关闭动作或多条消息队列 | 统一 Toast/Status 区域，保留直至用户确认，并支持多条消息 |
| UX-18 | P2 | 工作流编辑器 | 功能完整，但移动端、触控和键盘用户缺少完整替代路径 | 明确桌面端定位；为窄屏提供只读/表单化编辑路径 |
| UX-19 | P1 | `/admin/agents` 能力选择 | MCP 工具多时，所有服务器和工具会一次性铺满创建/编辑表单；没有搜索、按服务器折叠、已选计数或“只看可用”筛选 | 增加本地搜索、分组折叠、选中摘要和空态；保留失效绑定单独告警区 |
| UX-20 | P1 | `/admin/agents` 能力选择 | Skill 的工具建议只是原始逗号文本，未标明哪些建议工具不存在、停用或未被当前 Agent 勾选 | 解析并展示建议与实际授权的差异；不存在的建议给出警告，但仍不自动授权 |
| UX-21 | P2 | `/admin/agents` 能力选择 | 失效绑定需要展开具体 Agent、取消勾选并保存后才会移除；列表摘要只显示数量，不能直接定位完整对象 | 增加绑定详情和“一键清理失效绑定”，清理前展示影响摘要 |
| UX-22 | P2 | `/admin/mcp` 工具列 | 只展示前三个工具名，没有完整目录查看入口、输入 schema 或服务器连接状态详情 | 增加工具目录展开页，并同时展示发现时间、在线状态和调用所需参数 |
| UX-23 | P2 | `/admin/skills` 提示词管理 | 已有 prompt preview API，但管理页没有入口查看 Skill 将如何注入所选 Agent，也未展示被跳过原因 | 在 Skill 引用和 Agent 编辑区增加“预览生效提示词”，与运行时使用同一拼装结果 |

## P0 / P1 修复记录（2026-11-10 第二轮）

本轮按严重度处理 **P0 与 P1**：P2 保持原样记录，未在本轮改动。

| ID | 严重度 | 状态 | 落地方式 |
| --- | --- | --- | --- |
| UX-01 | P0 | 已修复 | 删除按钮改为**触发器**，单击只打开确认对话框（对象名 + 影响摘要 + 恢复入口）；删除前写快照，列表页给出一条性的「撤销」入口（默认 30 分钟窗口）。见 `services/admin_web/views/confirm.py`、`services/admin_web/deletion.py`、`services/admin_web/routes/undo.py`。 |
| UX-02 | P0 | 已修复 | 删除 / 停用前检查 `workflow_repo.agents_using`，被引用时按钮**禁用**并写明引用者；路由层再兜一次。见 `services/admin_web/routes/agents_write.py`。 |
| UX-03 | P1 | 已修复 | 校验失败不再丢内容：服务端把已填值与**字段级**错误带回原页面，字段标 `aria-invalid` 并在下方给出说明，同时渲染错误摘要。密钥类字段只写、不回显。见 `services/admin_web/form_state.py`、`services/admin_web/views/fields.py`。 |
| UX-04 | P1 | 已修复 | 连接下拉只列**启用**的端点；选中连接后模型框即时提示「留空时使用 <默认模型>」，无默认模型时同步变为必填。 |
| UX-05 | P1 | 已修复 | 在用的 LLM 端点 / MCP 服务器在行内显示**引用数量**与引用者，并禁用停用 / 删除，禁用原因写进 `title`。见 `views/llm.py`、`views/mcp.py` 的 `data-reference-count`。 |
| UX-06 | P1 | 已修复 | 各资源列表统一加**搜索 + 排序 + 分页**，筛选状态写进 URL（可分享）；分页在服务端做，单页条数有上限。Casbin 规则与平台管理员页也补上了搜索。见 `services/admin_web/listing.py`、`services/admin_web/views/list_controls.py`。 |
| UX-07 | P1 | 已修复 | 探测状态是 `role="status" aria-live="polite"` 的活动区，按钮带 `aria-busy`，失败时展开**排查指引**（401 / 404 / 超时的含义）。见 `views/llm.py` 的 `data-llm-help`。 |
| UX-08 | P1 | 已修复 | 未绑定连接的存量 Agent 显示**脱敏后的旧地址**（只留协议 + 主机）与「迁移到连接」提示，密钥仍只写。见 `views/agent_connection.py`。 |
| UX-09 | P1 | 已修复 | 行内 `<details>` 编辑换成**独立编辑面板**：同一时刻只展开一个对象，`?resource=<code>` 是深链接，面板内有「关闭」入口，并带**未保存离开保护**。见 `services/admin_web/views/edit_panel.py`。 |
| UX-19 | P1 | 已修复 | 能力选择器加**本地搜索**、**只看可用**、**已选计数**（`role=status`）与**分组折叠**；空分组自动隐藏。见 `views/agent_capabilities.py`、`static/admin-ui.js`。 |
| UX-20 | P1 | 已修复 | Skill 的工具建议被解析成逐条状态（已授权 / 未授权 / 已停用 / 不存在），不存在的建议给出警告但**不自动授权**。见 `views/agent_capabilities.py`。 |

### 顺带修掉的回归

- 删除 Agent 时快照里的 `temperature` 是 `DECIMAL`，直接塞进 JSON 列会抛
  `TypeError`——删除动作 500。现在快照统一转成 JSON 安全值
  （`services/admin_web/deletion.py` 的 `json_safe`），并有回归用例
  `tests/test_admin_ux_fixes.py::test_agent_with_decimal_temperature_can_be_deleted`。
- 迁移 `0019_admin_deleted_resource` 的时间列写成 `sa.DATETIME(fsp=3)`，
  在 PostgreSQL 上直接抛 `TypeError`。改用 `mysql.DATETIME(fsp=3)` 变体，
  与 `app/models/base.py` 的 `DATETIME3` 一致。
- 确认对话框原本是一个**没有 `display:none`** 的 `section`：页面上一直挂着
  一张空卡片。现在默认不渲染，打开时是覆盖全屏的遮罩层，并支持 Esc 关闭。
- 中英词表里有一处**正则写成了字符串**，导致 `pattern.test is not a function`，
  整个翻译流程抛错、中文界面整页失效。现在有 Node 用例守住词表结构
  （`tests/js/admin-ui-i18n.test.mjs`）。

## 已完成的联动改进

- Agent 创建与编辑改为选择已配置的 LLM 连接，不再重复填写 Base URL 和 API Key。
- 空模型自动采用所选连接的默认模型；无可用模型时拒绝保存。
- 运行期绑定 Agent 只使用所选连接；连接失效时显式报错，不静默切换。
- 被 Agent 引用的 LLM 连接禁止停用和删除，管理端与共享 API 使用同一边界。
- 修复 LLM 管理 API 未返回和未保存 `model` 的现有回归。
- Agent 的能力配置改为从已发布 Skill、内置工具和已发现 MCP 工具中显式勾选，不再手填逗号编码。
- Skill 的 `allowed_tools` 仅显示为建议，不自动授予工具权限；只有 Agent 上勾选的工具会进入授权集合。
- Skill、MCP 页面显示被哪些 Agent 使用；Agent 列表显示能力数量和失效绑定警告。
- 已启用且被 Agent 使用的 Skill 禁止停用、转草稿或归档；已停用的历史绑定仍允许维护内容，避免管理页永久锁死。
- MCP 服务器被 Agent 使用后禁止停用、删除和修改连接配置；工具目录刷新不能移除仍被绑定的工具。
- 停用的 LLM 连接不再出现在 Agent 下拉中；停用 MCP 服务器上的旧工具快照不能再绑定到 Agent。
- 删除或失效的历史绑定在读路径中保持宽容，不导致列表接口 500；编辑表单允许取消勾选后移除。
- 管理页的栅格子项与可横向滚动表格不再撑宽整页；窄屏下表格只在自身容器内滚动。
- 空 option 值显式渲染为 `value=""`，修复浏览器把翻译后显示文案当作提交值的问题。
- Agent 的 LLM 连接改为浏览器原生必填；无默认模型时模型框同步变为必填。
- MCP 表单按传输方式只展示对应地址或命令字段，并即时设置必填与禁用状态。
- LLM 探测状态增加 `role="status"`、`aria-live="polite"` 和按钮 `aria-busy`。
- 空 Skill 分组不再渲染空框；Skills、内置工具和失效绑定增加分组标题。
- 补齐 Agent 引导、表头、审计摘要、OAuth/平台管理员说明及删除确认的中文文案。
- 统一下拉框显示文案与提交值；审计“任意”筛选现在提交空字符串。

## 本轮验收记录（2026-11-10 第二轮，P0/P1）

- 管理端专项：`256 passed`（`test_admin_ux_fixes` 新增 10 条，逐条覆盖
  删除确认 + 撤销、字段级校验、列表搜索 / 排序 / 分页、编辑面板深链接、
  引用数量、探测播报、能力选择器与存量 Agent 脱敏展示）；工作流前端交互 +
  词表守卫：Node 测试 `77 passed`；`admin-ui.js` 语法检查通过。
- 迁移与边界用例另跑：`tests/test_migrations.py`、`tests/test_api_boundaries.py`
  `34 passed`（含新增的 `0019` 迁移）。
- 拆分路由模块时踩到的一个坑：探测接口的处理器搬到了 `routes/llm_write.py`，
  而用例把桩打在 `routes/llm.py` 上——桩打偏了，测试就会真去连外部服务。
  已让用例改打在**持有处理器的模块**上，并把这个替换点在代码里写明。
- admin 页面快照已按最终元素树重建，路由表快照同步更新（仅新增
  `POST /admin/undo/{entry_id}`）。
- 已部署到 `192.168.5.19:/docker/web-agent-stack`（docker compose，
  备份 `web-agent-stack-before-uxfix-20261110.tar.gz`）；`admin-web`、
  `web-client` 与 `studio` 均 healthy，`/admin/health` 与 `/api/health` 返回 200，
  迁移 `0019` 已应用。
- 内置浏览器已验证 14 个管理页：页面级横向溢出为 0，控制台错误/警告为 0，
  确认对话框不再在页面上留空卡片。
- 已实测：删除先弹确认对话框（对象名 + 影响摘要，中文）、删除后可撤销
  （删除 → 撤销 → 原样恢复）、校验失败保留输入并逐字段标错、列表搜索 /
  排序 / 分页（URL 可分享）、独立编辑面板与 `?resource=` 深链接、未保存离开
  保护、能力选择器的搜索 / 折叠 / 计数、在用端点的引用数量与禁用原因、
  默认模型提示、连接下拉只列启用项、中英文切换。
- 顺带修掉的体验问题：确认对话框不再在每页底部留一张空卡片（默认隐藏、
  打开时是全屏遮罩、Esc 可关）；列表排序的「默认」选项不再把中文文案
  `sort=默认` 写进 URL；编辑面板标题与撤销按钮的可访问名称一并中文化。


## P0 / P1 修复记录（2026-11-10 第三轮，内置浏览器实测）

第二轮的修复在**内置浏览器**里逐个按钮实测时，又暴露出几条**纸面评审没看出来**
的 P0/P1。本轮把它们补齐，并给每条都加了回归用例。

### 新发现的问题

| ID | 严重度 | 位置 | 问题 | 状态 |
| --- | --- | --- | --- | --- |
| UX-24 | **P0** | `deploy/docker-compose.stack.yml` | compose 的 `environment:` 用 `${ADMIN_DATABASE_URL:-}` 覆盖了 `env_file` 里的同名键。compose 的变量插值在**没有** `--env-file .env` 时取到空串，而空串**照样覆盖** `env_file`——于是 `ADMIN_DATABASE_URL` 被抹成空值，管理端静默退回客户端的 MySQL 主库（`ADMIN_PUBLIC_BASE_URL` 同样被改成 `http://127.0.0.1:8001`）。这直接违反 `deploy/README.md` 的「admin 与客户端配置独立数据库账号，限制客户端读取超级管理员密码摘要」。 | 已修复 |
| UX-25 | **P1** | `/admin/platform-admins` | 平台超级管理员的删除按钮是 `type="submit"` + 一个 `data-confirm` 属性。`data-confirm` 只被 i18n 当成待翻译文本读过，**没有任何脚本监听它**——所以这是「单击立即提交」，UX-01 在这一页根本没生效。 | 已修复 |
| UX-26 | **P1** | 工作流编排器 | 「保存工作流」是一次普通表单 POST：服务端校验不过就 303 跳回**列表页**，画布上还没保存的改动**全部丢失**，管理员只看到一句通用的「提交的值无效」。UX-03 的「保留输入 + 逐字段标错」没有覆盖到编排器。 | 已修复 |
| UX-27 | **P1** | `/admin/undo/{id}` | 撤销路由把权限页面写死成 `agents` / `agent_editor`：撤销一个 LLM 端点也要求 Agent 编辑权，撤销一个平台管理员更是完全不通。 | 已修复 |
| UX-28 | **P1** | `/admin/llm` 服务商预设 | 预设的自动填充用 `document.querySelector('form[action="/admin/llm"]')` 取表单。页面上有**两张** action 相同的表单——「搜索 / 排序」过滤表单（GET）和「创建端点」表单（POST）——`querySelector` 命中的是排在前面��过滤表单，于是预设把值写进了一个根本没有这些字段的表单。用户点预设**没有任何反应**。 | 已修复 |
| UX-29 | **P1** | `/admin/llm` 「测试连接」 | 服务端先探 `GET {base_url}/models`，成功后才探对话端点。而不少 OpenAI 兼容网关**不开放** `/models`（403/404），对话却完全正常——这类可用配置被误判成「连接失败」。 | 已修复 |
| UX-30 | **P1** | LLM 探测失败提示 | 探测失败把上游响应正文截断 200 字符后**原样回显**到页面：上游的 request_id、错误 JSON、甚至网关回显的密钥片段都会出现在管理端页面上。 | 已修复 |
| UX-31 | P2 | 行内按钮 | `title=disabled_reason or None` 在 ludic 里会把 Python 的 `None` 渲染成字符串，于是每一行按钮的 HTML 里都有 `title="None"`（再被 i18n 翻成 `title="无"`）。 | 已修复 |
| UX-32 | P2 | 中英词表 | 词表漏翻导致中文界面露出英文：确认对话框的 `Cancel`、撤销提示 `Resource restored.`、字段级校验文案、访问控制页的说明与注册表里的页面 / 组件描述、角色删除按钮、OAuth 提供方的启停按钮。 | 已修复 |

### 落地方式

- **UX-24**：删掉 `admin-web` 的 `environment:` 里对 `ADMIN_DATABASE_URL` /
  `ADMIN_PUBLIC_BASE_URL` / `ADMIN_SESSION_COOKIE_SECURE` 的覆盖，改为由
  `env_file: ../.env` 提供；部署命令固定为
  `docker compose -f deploy/docker-compose.stack.yml --env-file .env up -d --build`。
  部署后用 `docker exec webagent-admin env | grep ADMIN_DATABASE_URL` 复核。
- **UX-25**：改用 `delete_trigger`（确认对话框 + 对象名 + 影响摘要），并把
  平台管理员纳入快照撤销：`deletion.py` 新增 `super_admin_account` 快照类型
  （主键是用户名，快照统一落在 `code` 位），删除前留快照，列表页给「撤销删除」。
- **UX-26**：保存改成 `fetch`（`Accept: application/json`），服务端
  `workflows_update` 对 JSON 客户端回 `{"ok": false, "errors": [...]}`；
  校验逻辑抽到 `services/admin_web/workflow_save.py`。没有 JS 时仍然走原来的
  303（跳回编排页而不是列表页，画布不丢）。
- **UX-27**：撤销路由先读快照的资源类型，再按**那一类**的写权限判定。
- **UX-28**：预设下拉改按「有没有 `code` 字段」认出创建表单。
- **UX-29**：「测试连接」先探 `POST /chat/completions`，模型列表改成锦上添花；
  402/429 时再用模型列表确认「地址与密钥是好的」，继续标 `limited`。
- **UX-30**：上游错误只保留「状态码 + 归类说明」，不回显正文。
- **UX-31**：没有禁用原因时**不写** `title` 属性。
- **UX-32**：补齐词条与模式，并加 Node 用例守住词表结构与新增词条。

### 本轮验收记录（2026-11-10 第三轮）

- 管理端后端全量：`313 passed`（新增 `tests/test_admin_ux_fixes_round3.py`
  8 条，逐条覆盖上表的 UX-24/25/26/27/29/30）。
- 前端逻辑：`node --test tests/js/*.test.mjs` → `81 passed`
  （新增 `tests/js/admin-ui-llm-preset.test.mjs` 2 条守住 UX-28；
  `tests/js/admin-ui-i18n.test.mjs` 新增 1 条守住 UX-32 的词条）。
- 部署：`192.168.5.19:/docker/web-agent-stack`
  （备份 `web-agent-stack-before-round3-20261110.tar.gz`）；
  `admin-web`、`web-client`、`studio` 均 healthy，`/admin/health` 返回 200。
- 部署后复核 `ADMIN_DATABASE_URL` 已指向 PostgreSQL `webagent_admin`，
  `ADMIN_PUBLIC_BASE_URL` 已是 `http://192.168.5.19:8001/admin`。
- 内置浏览器：14 个管理页在 1516 px 与 454 px 两种视口下横向溢出均为 0，
  控制台错误 / 警告为 0；逐个按钮用鼠标触发实测（详见
  `admin-e2e-test-cases.md` 的「内置浏览器验收记录」）。


## P0 / P1 修复记录（2026-11-11 第四轮，内置浏览器逐按钮实测）

第三轮修完之后，把**每一个按钮**都用鼠标点了一遍（本轮全程用内置浏览器，
所有交互都走真实鼠标事件，没有用 JS 直接改 DOM 或调接口）。逐按钮实测
暴露出第四轮的问题——它们不是评审纸面能看出来的，全部要**操作一遍**
才会撞见。

### 新发现的问题

| ID | 严重度 | 位置 | 问题 | 状态 |
| --- | --- | --- | --- | --- |
| UX-33 | **P1** | `/admin/mcp` | 注册 stdio 类型的 MCP 服务器连不上时，字段级错误落在 **`url`** 上——而 stdio 模式下 `url` 那一栏是**禁用并隐藏**的。结果是管理员提交失败后**看不到任何字段标红**，只看到一句「连接无法校验。」，根本不知道错在哪一栏。根因：`_mcp_field_errors()` 不区分传输方式，所有兜底文案一律挂到 `url`。 | 已修复 |
| UX-34 | **P1** | `/admin/mcp` 「刷新工具」 | 目录刷新失败只有一句「无法完成操作。」，管理员分不清是**连不上服务器**还是**有工具仍被 Agent 占用**——这两种情况的处理方式完全相反（前者去改地址，后者去解绑 Agent）。 | 已修复 |
| UX-35 | **P1** | `/admin/preferences`、`/admin/platform-admins` | 保存偏好设置后重定向到 `?notice=saved`，但页面**从来不读这个参数**——管理员点了「保存偏好设置」，屏幕上**什么反馈都没有**。平台管理员撤销删除后同样没有「已恢复」的提示。 | 已修复 |
| UX-36 | **P1** | `/admin/audit` | 审计筛选条件**不回显**：URL 里带着 `?source=builtin&ok=true` 重新打开页面，两个下拉框却都停在「任意」。管理员看到的条件与实际生效的条件不一致，改一个条件再提交还会把另一个悄悄丢掉。 | 已修复 |
| UX-37 | **P1** | `/admin/llm` 探测失败 | 探测失败的服务端说明是**整句英文**（`GET /models failed: HTTP 401. The API key was rejected. ...`），中文界面里直接露出英文；折叠的「排查指引」正文也没进词表。 | 已修复 |
| UX-38 | P2 | 全局 flash | 七个列表页（Agent / LLM / MCP / Skill / 工作流 / OAuth 提供方 / 审计）把 `flash_from_query` 的**第二个返回值丢掉了**，于是错误提示与成功提示**样式完全一样**（都走 `flash info`），管理员分不出这是「成功了」还是「失败了」。 | 已修复 |
| UX-39 | P2 | `app/models/__init__.py` | `admin_deleted_resource`（撤销快照表）**没有**从 `app.models` 导出，于是 `Base.metadata` 里的表集合**取决于 import 顺序**：`test_postgres_compat` 单独跑通过、全量跑失败。三种方言的表一致性守卫因此变成了一颗定时炸弹。 | 已修复 |
| UX-40 | **P0** | `/admin/llm`、`/admin/agents`、`/admin/workflows` | 三处**漏 import**：`routes/llm_write.py` 用了 `LlmEndpointUpdate` 却没 import（「保存端点」一按就 500）；`routes/agents_write.py` 与 `routes/workflows_write.py` 用了 `as_bool` 却没 import（智能体与工作流的「禁用 / 启用」一按就 500）。这三条路径**没有任何测试覆盖**，单靠端到端用例抓不到。 | 已修复 |

### 落地方式

- **UX-33**：`_mcp_field_errors(exc, transport)` 增加传输方式参数——stdio 的
  兜底文案挂到 `command`，http 挂到 `url`；创建路由传 `submitted["transport"]`，
  更新路由传 `existing.transport`。顺带修了 `timeout_sec` 的整数解析错误
  （原来也会落到 `url` 上）。
- **UX-34**：`McpCatalogError` 增加 `reason` 分类（`unreachable` /
  `tools_in_use`），路由据此重定向到 `error=mcp_unreachable` /
  `error=mcp_tools_in_use`，词表补两条中文说明。
- **UX-35**：`preferences_get` 与 `platform_admins_get` 改成调用
  `flash_from_query(request)` 并把 `flash` / `flash_kind` 传给视图；
  词表补 `Preferences saved.`。
- **UX-36**：`audit_page` 接收 `run_id` / `tool_name` / `source` / `ok`
  四个当前值并回显到控件上；`audit_get` 把它们传下去。
- **UX-37**：`LlmProbeError` 增加稳定的 `code`（`http_401` / `network` /
  `invalid_response` …），探测接口在 JSON 里回 `error_code`；前端按分类用
  `PROBE_ERRORS` 词表本地化，英文界面保留服务端原文。折叠区里的排查指引
  正文补进 `ZH`。
- **UX-38**：七个路由把 `flash, _ =` 改成 `flash, flash_kind =`，视图签名
  补 `flash_kind` 并透传给 `shell()`。
- **UX-39**：`app/models/__init__.py` 导出 `AdminDeletedResource`，
  `test_postgres_compat.py` 的 `_EXPECTED_TABLES` 补上
  `admin_deleted_resource`。
- **UX-40**：补上三处漏掉的 import，并新增
  `tests/test_admin_route_static_checks.py`——用 `symtable` **静态**检查
  `services/admin_web` 整棵树上「引用了却没定义的全局名」。管理端的处理器
  都嵌在 `register()` 里面，所以检查必须**递归所有作用域**（只看一层会漏掉
  真正的漏 import）。这类漏 import 在运行到那行之前完全无害，只有真的按了
  那个按钮才会炸；现在它在提交之前就被挡下来了。

### 本轮验收记录（2026-11-11 第四轮）

- 后端全量：`pytest tests/ --ignore=tests/test_mcp_e2e.py` → **1114 passed**、
  30 skipped。仅 `test_studio_service.py::test_request_id_dedup_skips_second_run`
  失败，而它在**改动前的基线上同样失败**（本机 studio 执行的临时文件路径问题，
  与管理端无关）。`test_mcp_e2e.py` 在本机因为 venv 里 `pywin32` 的
  `_win32sysloader.dll` 缺失而无法导入 `mcp.server.stdio`，同样属**环境问题**。
- 管理端静态守卫：`pytest tests/test_admin_route_static_checks.py` → 77 passed。
- 前端逻辑：`node --test tests/js/*.test.mjs` → `83 passed`。
- 部署：`192.168.5.19:/docker/web-agent-stack`（docker compose，
  `--env-file .env`）；`admin-web` / `web-client` / `studio` 均 healthy，
  `/admin/health` 返回 200，`ADMIN_DATABASE_URL` 指向 PostgreSQL
  `webagent_admin`。
- 内置浏览器：14 个管理页逐页打开，**每一个按钮都用鼠标点过**（详见
  `admin-e2e-test-cases.md` 的「内置浏览器验收记录（第四轮）」）。
