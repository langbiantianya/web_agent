# Workflow 可视化编排器 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `/admin/workflows` 的手写 Definition JSON 编辑器替换为拖拽画布编排器，支持节点拖拽、箭头连线、属性检查器、实时校验、撤销重做与自动布局，且不新增前端依赖、不改数据库、不改客户端 API 契约。

**Architecture:** 三层。后端 `app/core/workflow_def.py` 把图结构校验从「抛第一条错」改造成「产出带定位的结构化 issue」，并由新端点 `POST /admin/workflows/validate` 暴露——**校验规则仍然只有一份**。中间层 `workflow-editor-core.js` 是无 DOM 依赖的纯逻辑（模型转换、撤销栈、分层布局），可用 `node --test` 直接测。外层 `workflow-editor.js` 只做 SVG 渲染与事件绑定，所有决策都委托给 core。定义数据本身仍是唯一的真相：画布 ⇄ definition 双向转换，画布不持有第二份状态。

**Tech Stack:** Python 3.14 / Ludic SSR / Starlette / pydantic v2 / 原生 SVG + CSS / 原生 ES2020 JS（无 npm、无构建步骤）/ `node --test`（Node ≥ 18 内置）

## Global Constraints

以下约束对**每个任务**都成立：

- **不引入任何前端依赖**：不新增 `package.json`、不引入 npm 包、不使用 CDN、不引入前端构建步骤。admin-web 是 Ludic SSR + 原生 JS/CSS，静态资源走已有的 `/admin/static` 挂载。
- **不新增数据库迁移**：`definition` 是 JSON 列，`label`（节点级）与 `layout`（顶层）都靠 JSON 承载。
- **客户端 API 契约不变**：`/api/workflows` 仍然接收完整 `definition`，行为与响应结构一字不改。只有后台的人机界面变。
- **校验规则只有一份**：图结构规则的唯一实现在 `app/core/workflow_def.py`。客户端 JS **不得**重新实现环检测、孤立节点、条件边混用、表达式语法这些规则；JS 只做「空节点列表」这类零成本的前置提示，权威判定一律来自服务端。
- **写操作必须走既有鉴权链**：所有 POST 都要经过 `_origin_ok` + `_require_csrf`（Cookie/请求体/数据库 Session 三方一致）+ `_can_edit_resource`（页面可见性 + 组件权限）。新增端点不得绕过。
- **既有错误消息逐字不变**：`WorkflowDefinition` 抛出的 `ValueError` 文本在本次改造前后必须完全一致，`tests/test_workflow_def.py` 的既有断言不允许修改。
- **画布脚本只在编排页加载**：`/admin/workflows` 列表页不得加载 `workflow-editor*.js` / `workflow-editor.css`。
- **本地 `.venv` 不可用**（`cffi` 为 cp314 编译、当前解释器是 3.15，且 pip 无网络）。Python 测试一律在服务器测试镜像里跑：

  ```bash
  # 服务器 100.114.77.27
  cd /docker/web-agent-stack && docker run --rm --network host \
    -v /docker/web-agent-stack:/src -w /src webagent/tests:oauth-fix \
    python -m pytest tests/test_admin_web.py tests/test_workflow_def.py -q -p no:langsmith --timeout=300
  ```

  改完服务端源码后必须重建测试镜像再跑（见 Task 2 Step 0）。

- **JS 测试在本地跑**：`node --test tests/js/workflow-editor-core.test.mjs` （本机 Node v24）。

---

## 计划自检结论（写完计划后实跑过）

这份计划的代码**不是写完就算**：写完后把计划里的代码块抽出来实跑了一遍，结果如下。

- **core.js + 其测试**：从计划里抽出 `workflow-editor-core.js` 与
  `workflow-editor-core.test.mjs`，用 `node --test` 实跑 → **11 passed / 0 failed**。
  过程中修掉了两处真问题：
  1. 原本用 `node:vm` 沙箱加载 core.js，但 vm 里造出的对象带**另一个 realm 的
     `Object.prototype`**，`assert/strict` 的 `deepStrictEqual` 会以「结构相同
     但原型不同」失败。改成 `new Function(source + "return globalThis.WorkflowEditorCore;")()`
     在当前 realm 求值。
  2. `computeLayout` 的分层断言原本方向写反了（把第 0 层和第 1 层断言成同列）。
- **workflow-editor.js**：把基础实现与 12 个功能片段拼成一个完整文件，
  `node --check` → 通过。
- **Python 代码块**：26 个块逐个 `ast.parse`。剩余报错全部是**故意的片段**
  （形如「把这段插进某个函数体内」，本身就带缩进），不是缺陷。过程中修掉了两处真问题：
  1. `_detect_template_problems` 里的 f-string 大括号数写错了——原文是
     **5 个左括号**（`{{{{{dotted[0]}}}`），少写一个会变成语法错误。计划里现在
     直接引用 `app/core/workflow_def.py` 现有三行的原文。**改这段时别手敲大括号。**
  2. `pre(...)` 原本写成「先关键字参数、后位置参数」，Ludic 的组件签名是
     `(*children, **attrs)`，位置参数必须在前。

> 结论：计划里的 JS 逻辑与测试断言是**跑通过的**，不是纸面推演。

---

## File Structure

| 文件 | 责任 | 任务 |
| --- | --- | --- |
| `app/core/workflow_def.py` | 图定义模型与**唯一**的图结构校验实现 | Task 1, 2 |
| `services/admin_web/views.py` | Ludic 页面渲染：列表页改造、编排页 | Task 4 |
| `services/admin_web/app.py` | 路由：编排页 GET、校验 POST、创建端点改造 | Task 3, 4 |
| `services/admin_web/static/workflow-editor-core.js` | **纯逻辑**：definition ⇄ 模型、撤销栈、分层布局、节点增删连。无 DOM | Task 5 |
| `services/admin_web/static/workflow-editor.js` | DOM/SVG 渲染与事件绑定，委托决策给 core | Task 6, 7, 8 |
| `services/admin_web/static/workflow-editor.css` | 画布 / 节点 / 检查器 / 错误条样式，复用现有 CSS 变量 | Task 6 |
| `tests/test_workflow_def.py` | `label`、`layout`、`collect_graph_issues` 的用例 | Task 1, 2 |
| `tests/test_admin_web.py` | 编排页、校验端点、创建改造、往返保存的用例 | Task 3, 4 |
| `tests/js/workflow-editor-core.test.mjs` | core.js 纯逻辑用例（`node --test`） | Task 5 |
| `docs/design/TODO.md` | 记录「手写 JSON 编辑」退役 | Task 9 |
| `services/admin_web/README.md` | 「当前页面」清单补编排页 | Task 9 |

**文件边界约定**：`core.js` 里出现 `document` / `window` / `DOMParser` 即视为越界——它必须能在 `node:vm` 里裸跑。DOM 事件处理、SVG 元素创建、坐标换算一律留在 `workflow-editor.js`。

---

## Task 1: 节点 `label` 字段与 definition 顶层 `layout`

**Files:**
- Modify: `app/core/workflow_def.py`（`WorkflowNode` 定义处，约第 47–67 行）
- Test: `tests/test_workflow_def.py`

**Interfaces:**
- Consumes: 无（起点任务）
- Produces:
  - `WorkflowNode.label: str`，默认 `""`，最大长度 128，仅用于展示，运行期不读
  - `WorkflowDefinition` 继续 `extra="allow"`，因此 `layout` 顶层键会被 `model_dump(by_alias=True, exclude_none=True)` 原样带过

- [ ] **Step 1: 写失败测试**

  在 `tests/test_workflow_def.py` 末尾追加：

  ```python
  def test_workflow_node_accepts_optional_label():
      """节点可带展示用 label，且缺省时为空串。"""
      node = WorkflowNode(id="step1", type="agent", agentCode="planner")
      assert node.label == ""
      assert WorkflowNode(id="s", type="agent", agentCode="p", label="意图识别").label == (
          "意图识别"
      )


  def test_workflow_definition_keeps_top_level_layout_key():
      """definition 顶层 layout 坐标键必须原样保留，不被校验丢掉。"""
      payload = {
          "entry": "step1",
          "nodes": [{"id": "step1", "type": "agent", "agentCode": "planner"}],
          "edges": [],
          "layout": {
              "nodes": {"step1": {"x": 80, "y": 120}},
              "viewport": {"x": 0, "y": 0, "scale": 1},
          },
      }
      definition = WorkflowDefinition.model_validate(payload)
      dumped = definition.model_dump(by_alias=True, exclude_none=True)
      assert dumped["layout"]["nodes"]["step1"] == {"x": 80, "y": 120}
      assert dumped["layout"]["viewport"]["scale"] == 1
  ```

  若 `tests/test_workflow_def.py` 顶部尚未导入 `WorkflowNode`，补上：

  ```python
  from app.core.workflow_def import (
      WorkflowDefinition,
      WorkflowEdge,
      WorkflowNode,
  )
  ```

- [ ] **Step 2: 跑测试确认失败**

  ```bash
  python -m pytest tests/test_workflow_def.py -q -k "label or layout"
  ```

  （本地 venv 坏掉时改用 Global Constraints 里的服务器命令。）

  Expected: FAIL —— `WorkflowNode` 没有 `label` 参数，且 `model_dump` 丢掉 `layout`。

- [ ] **Step 3: 实现**

  在 `app/core/workflow_def.py` 的 `WorkflowNode` 里，`type` 字段之后插入：

  ```python
      # 展示用名称。运行期不读它（产出键仍取 outputKey 或 id），
      # 但画布上必须靠它区分两个同名的审批节点。
      label: str = Field(default="", max_length=128)
  ```

- [ ] **Step 4: 跑测试确认通过**

  ```bash
  python -m pytest tests/test_workflow_def.py -q
  ```

  Expected: PASS，且 `tests/test_workflow_def.py` 的**既有**用例全部仍然通过。

- [ ] **Step 5: 提交**

  ```bash
  git add app/core/workflow_def.py tests/test_workflow_def.py
  git commit -m "feat: add optional workflow node label"
  ```

---

## Task 2: 结构化图校验 `collect_graph_issues`

**Files:**
- Modify: `app/core/workflow_def.py`
- Test: `tests/test_workflow_def.py`

**Interfaces:**
- Consumes: Task 1 的 `WorkflowNode.label`
- Produces:
  ```python
  @dataclass(frozen=True)
  class GraphIssue:
      message: str
      node_ids: tuple[str, ...] = ()
      edge_refs: tuple[str, ...] = ()      # 形如 "from->to"

  def collect_graph_issues(payload: dict) -> list[GraphIssue]: ...
  ```
  规则、检查顺序、消息文本与改造前**逐字一致**。

- [ ] **Step 0: 重建服务器测试镜像**

  ```bash
  ssh -o BatchMode=yes root@100.114.77.27 \
    'cd /docker/web-agent-stack && docker build -f Dockerfile.test -t webagent/tests:oauth-fix .'
  ```

- [ ] **Step 1: 写失败测试**

  追加到 `tests/test_workflow_def.py`：

  ```python
  def test_collect_graph_issues_reports_all_problems_with_targets():
      """一次收集全部问题，并给每条问题标出节点或边。"""
      payload = {
          "entry": "step1",
          "nodes": [
              {"id": "step1", "type": "agent", "agentCode": "planner"},
              {"id": "review", "type": "agent", "agentCode": "writer"},
              {"id": "lonely", "type": "agent", "agentCode": "writer"},
          ],
          "edges": [
              {"from": "step1", "to": "review", "when": "upstream.nope == \"x\""},
          ],
      }
      issues = collect_graph_issues(payload)
      messages = [issue.message for issue in issues]
      assert any("lonely" in m for m in messages)
      assert any("upstream.nope" in m for m in messages)
      orphan = next(i for i in issues if "lonely" in i.message)
      assert "lonely" in orphan.node_ids
      bad_edge = next(i for i in issues if "upstream.nope" in i.message)
      assert "step1->review" in bad_edge.edge_refs


  def test_collect_graph_issues_is_empty_for_valid_graph():
      """合法图没有 issue。"""
      payload = {
          "entry": "a",
          "nodes": [
              {"id": "a", "type": "agent", "agentCode": "planner"},
              {"id": "b", "type": "agent", "agentCode": "writer"},
          ],
          "edges": [{"from": "a", "to": "b"}],
      }
      assert collect_graph_issues(payload) == []


  def test_validate_graph_message_unchanged_after_refactor():
      """改造后 WorkflowDefinition 抛出的消息与既有断言逐字一致。"""
      payload = {
          "entry": "a",
          "nodes": [
              {"id": "a", "type": "agent", "agentCode": "planner"},
              {"id": "b", "type": "agent", "agentCode": "writer"},
          ],
          "edges": [{"from": "a", "to": "b", "when": "upstream.missing == \"x\""}],
      }
      with pytest.raises(ValidationError) as excinfo:
          WorkflowDefinition.model_validate(payload)
      text = str(excinfo.value)
      assert "upstream.missing" in text
      assert "不存在" in text
  ```

  顶部补 import：

  ```python
  import pytest
  from pydantic import ValidationError
  from app.core.workflow_def import collect_graph_issues
  ```

  > 注意：既有用例里若已有 `import pytest`，不要重复添加。

- [ ] **Step 2: 跑测试确认失败**

  ```bash
  python -m pytest tests/test_workflow_def.py -q -k "collect_graph_issues"
  ```

  Expected: FAIL —— `ImportError: cannot import name 'collect_graph_issues'`。

- [ ] **Step 3: 实现 `GraphIssue` 与 `collect_graph_issues`**

  在 `app/core/workflow_def.py` 顶部加 import：

  ```python
  from dataclasses import dataclass, field
  ```

  在 `WorkflowDefinition` **之前**插入：

  ```python
  @dataclass(frozen=True)
  class GraphIssue:
      """一条图结构问题，附带定位信息。

      ``message`` 与改造前 ``ValueError`` 的文本逐字一致——它是给管理员看的
      唯一文案来源，前端不另造说法。

      ``node_ids`` / ``edge_refs`` 让画布能把错误高亮到具体节点或边，
      而不是让用户在一句中文里自己找名字。
      """

      message: str
      node_ids: tuple[str, ...] = ()
      edge_refs: tuple[str, ...] = ()
  ```

  然后把 `WorkflowDefinition` 里的图结构检查改造成「产出 issue」的形态。**关键约束：检查顺序与消息文本一字不改**，只把 `raise ValueError(...)` 换成 `issues.append(GraphIssue(...))`。

  具体做法——把现有 `_validate_graph` 及其私有检查方法整体搬到**模块级函数**
  `collect_graph_issues(payload: dict) -> list[GraphIssue]` 里，方法体改成操作
  `issues` 列表：

  ```python
  def collect_graph_issues(payload: dict) -> list[GraphIssue]:
      """收集 definition 的全部图结构问题。

      规则、检查顺序与错误文案与 ``WorkflowDefinition`` 校验期完全一致；
      区别只在于这里**收集全部**问题并带上定位，而不是遇到第一条就抛。
      """
      issues: list[GraphIssue] = []
      ids = {str(n.get("id")) for n in payload.get("nodes") or []}
      entry = str(payload.get("entry") or "")
      edges = payload.get("edges") or []

      for edge in edges:
          src = str(edge.get("from") or "")
          dst = str(edge.get("to") or "")
          if src not in ids or dst not in ids:
              issues.append(
                  GraphIssue(
                      message=f"edge 引用了不存在的节点：{src} -> {dst}",
                      node_ids=tuple(sorted({src, dst} - ids)),
                      edge_refs=(f"{src}->{dst}",),
                  )
              )
      if entry and entry not in ids:
          issues.append(
              GraphIssue(
                  message=f"entry 节点不存在：{entry}",
                  node_ids=(entry,),
              )
          )

      issues.extend(_detect_cycle(payload))
      issues.extend(_detect_orphans(payload))
      issues.extend(_detect_fanin_shape(payload))
      issues.extend(_detect_outgoing_mixed(payload))
      issues.extend(_detect_condition_problems(payload))
      issues.extend(_detect_template_problems(payload))
      return issues
  ```

  配套的模块级私有函数（每条对应现有一个私有检查方法，**消息文本照抄**）：

  ```python
  def _detect_cycle(payload: dict) -> list[GraphIssue]:
      """检测环。消息与原 ``_validate_graph`` 一致。"""
      ids = [str(n.get("id")) for n in payload.get("nodes") or []]
      adj: dict[str, list[str]] = {i: [] for i in ids}
      indeg: dict[str, int] = {i: 0 for i in ids}
      for edge in payload.get("edges") or []:
          src = str(edge.get("from") or "")
          dst = str(edge.get("to") or "")
          if src in adj and dst in adj:
              adj[src].append(dst)
              indeg[dst] += 1
      ready = [i for i in ids if indeg[i] == 0]
      seen = 0
      while ready:
          cur = ready.pop(0)
          seen += 1
          for nxt in adj[cur]:
              indeg[nxt] -= 1
              if indeg[nxt] == 0:
                  ready.append(nxt)
      if seen != len(ids):
          return [GraphIssue(message="definition 存在环，无法确定执行顺序")]
      return []


  def _detect_orphans(payload: dict) -> list[GraphIssue]:
      """检测从 entry 走不到的孤立节点。"""
      ids = {str(n.get("id")) for n in payload.get("nodes") or []}
      if not ids:
          return []
      nodes = payload.get("nodes") or []
      entry = str(payload.get("entry") or "") or str(nodes[0].get("id"))
      adj: dict[str, list[str]] = {i: [] for i in ids}
      for edge in payload.get("edges") or []:
          src = str(edge.get("from") or "")
          dst = str(edge.get("to") or "")
          if src in adj and dst in adj:
              adj[src].append(dst)
      seen = {entry}
      queue = [entry]
      while queue:
          cur = queue.pop()
          for nxt in adj.get(cur, []):
              if nxt not in seen:
                  seen.add(nxt)
                  queue.append(nxt)
      orphans = ids - seen
      if orphans:
          return [
              GraphIssue(
                  message=(
                      "以下节点从 entry 走不到（孤立节点）："
                      + "、".join(sorted(orphans))
                  ),
                  node_ids=tuple(sorted(orphans)),
              )
          ]
      return []


  def _detect_fanin_shape(payload: dict) -> list[GraphIssue]:
      """检测汇聚节点入边混条件边。消息与原实现一致。"""
      ids = {str(n.get("id")) for n in payload.get("nodes") or []}
      incoming: dict[str, list[dict]] = {i: [] for i in ids}
      for edge in payload.get("edges") or []:
          dst = str(edge.get("to") or "")
          if dst in incoming:
              incoming[dst].append(edge)
      issues: list[GraphIssue] = []
      for node_id, edges in incoming.items():
          if len(edges) < 2:
              continue
          conditional = [e for e in edges if e.get("when")]
          if not conditional:
              continue
          refs = tuple(
              f"{e.get('from')}->{node_id}" for e in conditional
          )
          if len(conditional) < len(edges):
              bad = "、".join(f"{e.get('from')} --when--> {node_id}" for e in conditional)
              issues.append(
                  GraphIssue(
                      message=(
                          f"节点 {node_id} 是汇聚节点（{len(edges)} 条入边），"
                          f"但入边里混了条件边（{bad}）：条件不成立时它会永远等不到。"
                          f"请把入边全部改成无条件边，或让条件节点直接连到 {node_id} 的下游。"
                      ),
                      node_ids=(node_id,),
                      edge_refs=refs,
                  )
              )
          else:
              issues.append(
                  GraphIssue(
                      message=(
                          f"节点 {node_id} 的 {len(edges)} 条入边全是条件边："
                          f"本版不支持「多条条件边汇聚到同一节点」，请改成无条件汇聚。"
                      ),
                      node_ids=(node_id,),
                      edge_refs=refs,
                  )
              )
      return issues


  def _detect_outgoing_mixed(payload: dict) -> list[GraphIssue]:
      """检测同一节点出边混用条件与无条件。"""
      outgoing: dict[str, list[dict]] = {}
      for edge in payload.get("edges") or []:
          outgoing.setdefault(str(edge.get("from") or ""), []).append(edge)
      issues: list[GraphIssue] = []
      for node_id, edges in outgoing.items():
          conditional = [e for e in edges if e.get("when")]
          if conditional and len(conditional) < len(edges):
              issues.append(
                  GraphIssue(
                      message=(
                          f"节点 {node_id} 的出边里条件边与无条件边混用："
                          f"请统一成「全无条件（并行）」或「全条件（分支）」。"
                      ),
                      node_ids=(node_id,),
                  )
              )
      return issues


  def _detect_condition_problems(payload: dict) -> list[GraphIssue]:
      """检测 when 表达式的语法错误与未知变量。"""
      keys = {
          str(n.get("outputKey") or n.get("id"))
          for n in payload.get("nodes") or []
      }
      issues: list[GraphIssue] = []
      for edge in payload.get("edges") or []:
          when = str(edge.get("when") or "")
          if not when:
              continue
          src = str(edge.get("from") or "")
          dst = str(edge.get("to") or "")
          ref = f"{src}->{dst}"
          where = f"边 {src} -> {dst}"
          try:
              expr.check_syntax(when)
          except expr.ExprError as exc:
              issues.append(
                  GraphIssue(
                      message=f"{where} 的 when 表达式有语法错误：{exc}",
                      node_ids=(src, dst),
                      edge_refs=(ref,),
                  )
              )
              continue
          for path in expr.variables(when):
              if path[0] == "user_input":
                  continue
              if path[0] != "upstream":
                  hint = f"；请写成 upstream.{path[0]}" if path[0] in keys else ""
                  issues.append(
                      GraphIssue(
                          message=(
                              f"{where} 的 when 引用了未知变量 {path[0]}{hint}；"
                              f"可用：user_input、upstream.<outputKey>"
                          ),
                          node_ids=(src, dst),
                          edge_refs=(ref,),
                      )
                  )
                  break
              if len(path) == 1:
                  continue
              if path[1] not in keys:
                  issues.append(
                      GraphIssue(
                          message=(
                              f"{where} 的 when 引用了不存在的 upstream.{path[1]}；"
                              f"本图可用的 outputKey：{'、'.join(sorted(keys))}"
                          ),
                          node_ids=(src, dst),
                          edge_refs=(ref,),
                      )
                  )
                  break
      return issues


  def _detect_template_problems(payload: dict) -> list[GraphIssue]:
      """检测 inputTemplate 里的未知变量与点号写法。"""
      template = str(payload.get("inputTemplate") or "")
      if not template:
          return []
      dotted = re.findall(r"\{\{\s*([\w.]+)\s*\.\w+\s*\}\}", template)
      if dotted:
          return [
              GraphIssue(
                  message=(
                      f"inputTemplate 里的 {{{{{dotted[0]}.xxx}}}} 不支持点号访问；"
                      f"单独引用某个上游产出请直接写 {{{{{dotted[0]}}}}}，"
                      f"全部上游写 {{{{upstream}}}}"
                  )
              )
          ]
      known = {"user_input", "upstream"} | {
          str(n.get("outputKey") or n.get("id")) for n in payload.get("nodes") or []
      }
      used = set(_VAR_RE.findall(template))
      unknown = sorted(used - known)
      if unknown:
          return [
              GraphIssue(
                  message=(
                      f"inputTemplate 引用了未知变量：{'、'.join(unknown)}；"
                      f"可用：user_input、upstream、"
                      f"{'、'.join(sorted(known - {'user_input', 'upstream'})) or '（本图暂无上游产出）'}"
                  )
              )
          ]
      return []
  ```

- [ ] **Step 4: 让 `WorkflowDefinition` 复用同一份规则**

  把 `WorkflowDefinition._validate_graph` 改成：

  ```python
      @model_validator(mode="after")
      def _validate_graph(self) -> WorkflowDefinition:
          """model 级校验：一次把「这张图能不能跑」判干净。

          规则实现在 :func:`collect_graph_issues` 里——**只有一份**。
          这里只负责把它转成 pydantic 能包住的 ``ValueError``，且消息文本与
          改造前逐字一致，既有测试断言不需要改。
          """
          payload = self.model_dump(by_alias=True, exclude_none=True)
          issues = collect_graph_issues(payload)
          if issues:
              raise ValueError(issues[0].message)
          return self
  ```

  > **注意**：`collect_graph_issues` 里的「entry 为空时取第一个节点」与
  > `WorkflowDefinition.entry_id` 的取值一致，因此孤立节点检测的基准不变。
  >
  > **注意**：`model_dump(by_alias=True)` 会把节点的 `from_` 输出成 `from`，
  > 与 `collect_graph_issues` 读取的键名一致。

  同时删除已经被搬走的私有方法 `_check_reachable` / `_check_fanin_shape` /
  `_check_outgoing_uniform` / `_validate_conditions` / `_validate_template_vars`
  以及旧 `_validate_graph` 里内联的边端点 / entry 存在性检查（这些逻辑已经
  在 `collect_graph_issues` 里）。

  > **保留不动**：`_topo_order`、`entry_id`、`node_by_id`、`topo_order`、
  > `template_vars`、`render_input`。
  >
  > **已核实**：这五个私有方法**没有任何测试或外部代码直接调用**，
  > 删除是安全的。

- [ ] **Step 4b: 修掉指向被删方法的 docstring 交叉引用**

  删除私有方法后，有四处 Sphinx 交叉引用会指向不存在的成员，必须一起改掉
  （否则文档构建出现坏引用）：

  | 文件 | 原文 | 改成 |
  | --- | --- | --- |
  | `app/core/run_graph.py` | `:meth:`WorkflowDefinition._check_outgoing_uniform`` | `:func:`collect_graph_issues`` |
  | `app/core/run_graph.py` | `:meth:`WorkflowDefinition._validate_conditions`` | `:func:`collect_graph_issues`` |
  | `app/core/expr.py` | `:meth:`WorkflowDefinition._validate_conditions`` | `:func:`collect_graph_issues`` |
  | `app/core/workflow_def.py` | `:meth:`_validate_template_vars``（在 `template_vars` 的 docstring 里） | `:func:`collect_graph_issues`` |

  改完跑一遍：

  ```bash
  grep -rn "_check_outgoing_uniform\|_validate_conditions\|_validate_template_vars\|_check_reachable\|_check_fanin_shape" app/ tests/
  ```

  Expected: 无输出。

- [ ] **Step 5: 跑测试确认通过**

  ```bash
  python -m pytest tests/test_workflow_def.py -q
  ```

  Expected: 全部 PASS，**既有断言零修改**。

- [ ] **Step 6: 提交**

  ```bash
  git add app/core/workflow_def.py tests/test_workflow_def.py
  git commit -m "refactor: collect workflow graph issues with targets"
  ```

---

## Task 3: `POST /admin/workflows/validate` 校验端点

**Files:**
- Modify: `services/admin_web/app.py`（在 `workflows_post` 之前插入新路由）
- Test: `tests/test_admin_web.py`

**Interfaces:**
- Consumes: Task 2 的 `collect_graph_issues(payload) -> list[GraphIssue]`
- Produces:
  ```http
  POST /admin/workflows/validate
  Content-Type: application/json
  Body: {"csrf_token": "<csrf>", "definition": { ...definition... }}

  200 {"ok": true}
  200 {"ok": false, "errors": [{"message": "...", "node": "review", "edge": "step1->review"}]}
  403 {"ok": false, "errors": [{"message": "forbidden"}]}
  ```
  `node` 取 `node_ids` 的第一个，`edge` 取 `edge_refs` 的第一个；两者都没有时
  该字段缺省。

- [ ] **Step 1: 写失败测试**

  追加到 `tests/test_admin_web.py`：

  ```python
  async def test_admin_workflow_validate_endpoint_reports_graph_issues():
      """校验端点一次返回全部问题，并带出节点/边定位。"""
      client, _, _, csrf = await _create_seeded_logged_in_super_admin()
      try:
          payload = {
              "csrf_token": csrf,
              "definition": {
                  "entry": "step1",
                  "nodes": [
                      {"id": "step1", "type": "agent", "agentCode": "planner"},
                      {"id": "review", "type": "agent", "agentCode": "writer"},
                      {"id": "lonely", "type": "agent", "agentCode": "writer"},
                  ],
                  "edges": [
                      {"from": "step1", "to": "review", "when": 'upstream.nope == "x"'},
                  ],
              },
          }
          response = await client.post("/admin/workflows/validate", json=payload)
          assert response.status_code == 200, response.text
          body = response.json()
          assert body["ok"] is False
          messages = " ".join(item["message"] for item in body["errors"])
          assert "lonely" in messages
          assert "upstream.nope" in messages
          assert any(item.get("node") == "lonely" for item in body["errors"])
          assert any(
              item.get("edge") == "step1->review" for item in body["errors"]
          )
      finally:
          await client.aclose()


  async def test_admin_workflow_validate_endpoint_accepts_valid_definition():
      """合法定义返回 ok=true。"""
      client, _, _, csrf = await _create_seeded_logged_in_super_admin()
      try:
          response = await client.post(
              "/admin/workflows/validate",
              json={
                  "csrf_token": csrf,
                  "definition": {
                      "entry": "a",
                      "nodes": [
                          {"id": "a", "type": "agent", "agentCode": "planner"},
                          {"id": "b", "type": "agent", "agentCode": "writer"},
                      ],
                      "edges": [{"from": "a", "to": "b"}],
                  },
              },
          )
          assert response.status_code == 200
          assert response.json() == {"ok": True}
      finally:
          await client.aclose()


  async def test_admin_workflow_validate_requires_csrf():
      """缺 CSRF 时拒绝。"""
      client, _, _, _ = await _create_seeded_logged_in_super_admin()
      try:
          response = await client.post(
              "/admin/workflows/validate",
              json={
                  "definition": {
                      "entry": "a",
                      "nodes": [{"id": "a", "type": "agent", "agentCode": "p"}],
                      "edges": [],
                  }
              },
          )
          assert response.status_code == 403
          assert response.json()["ok"] is False
      finally:
          await client.aclose()
  ```

- [ ] **Step 2: 跑测试确认失败**

  ```bash
  python -m pytest tests/test_admin_web.py -q -k "workflow_validate"
  ```

  Expected: FAIL —— 404（路由不存在）。

- [ ] **Step 3: 实现端点**

  在 `services/admin_web/app.py` 中，`from app.core.workflow_def import ...` 一带
  补 import（若尚未导入）：

  ```python
  from app.core.workflow_def import collect_graph_issues
  ```

  在 `@app.post("/admin/workflows")` **之前**插入路由：

  ```python
  @app.post("/admin/workflows/validate")
  async def workflows_validate(request: Request):
      """校验一份 Workflow definition，不落库。

      画布用它做实时反馈。规则实现在 ``app.core.workflow_def``，与保存时
      走的是同一份代码——前端不自己判断「这个图合不合法」。
      """
      values = await _with_auth(request)
      if values is None:
          return _unauthorized()
      db, principal, _pref, _csrf = values
      try:
          try:
              body = await request.json()
          except (json.JSONDecodeError, ValueError):
              return JSONResponse(
                  {"ok": False, "errors": [{"message": "invalid_json"}]},
                  status_code=400,
              )
          if not isinstance(body, dict):
              return JSONResponse(
                  {"ok": False, "errors": [{"message": "invalid_json"}]},
                  status_code=400,
              )
          definition = body.get("definition")
          if not isinstance(definition, dict):
              return JSONResponse(
                  {"ok": False, "errors": [{"message": "definition_required"}]},
                  status_code=400,
              )
          authorized = (
              _origin_ok(request)
              and await _can_edit_resource(
                  db,
                  principal,
                  page_code="workflows",
                  component_code="workflow_editor",
              )
              and await _require_csrf(
                  db, request, principal, {"csrf_token": body.get("csrf_token")}
              )
          )
          if not authorized:
              return JSONResponse(
                  {"ok": False, "errors": [{"message": "forbidden"}]},
                  status_code=403,
              )
          issues = collect_graph_issues(definition)
          if not issues:
              return JSONResponse({"ok": True})
          errors = []
          for issue in issues:
              item: dict[str, str] = {"message": issue.message}
              if issue.node_ids:
                  item["node"] = issue.node_ids[0]
              if issue.edge_refs:
                  item["edge"] = issue.edge_refs[0]
              errors.append(item)
          return JSONResponse({"ok": False, "errors": errors})
      finally:
          await db.close()
  ```

  > `app.py` 顶部已 `import json`（`_json_object` 在用），`JSONResponse` 已在
  > `from starlette.responses import (...)` 里。缺哪个补哪个。

- [ ] **Step 4: 跑测试确认通过**

  ```bash
  python -m pytest tests/test_admin_web.py -q -k "workflow_validate"
  ```

  Expected: PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add services/admin_web/app.py tests/test_admin_web.py
  git commit -m "feat: add workflow definition validation endpoint"
  ```

---

## Task 4: 编排页与列表页改造（后端渲染）

**Files:**
- Modify: `services/admin_web/views.py`（`shell` 加参数；`workflows_page` 改造；新增 `workflow_editor_page`）
- Modify: `services/admin_web/app.py`（新增 `GET /admin/workflows/{code}`；创建端点改造）
- Test: `tests/test_admin_web.py`

**Interfaces:**
- Consumes: Task 3 的校验端点（前端用）；Task 1 的 `label` 与 `layout`
- Produces:
  - `shell(..., extra_scripts: list[str] | None = None)` —— 追加 `<script src=...>`
  - `workflow_editor_page(*, principal, csrf, pages, workflow, agents, theme, color_mode, density, can_write, flash=None) -> Any`
  - `GET /admin/workflows/{code}` —— 200 渲染画布页；无权限或不存在 404
  - `POST /admin/workflows` —— 表单字段改为 `code` / `name` / `description` / `agent_code` / `enabled`，服务端生成骨架 definition，创建后 303 跳 `/admin/workflows/{code}`
  - `POST /admin/workflows/{code}/update` —— 新增可选表单字段 `next`，保存后跳转到 `_safe_next(next)`（默认 `/admin/workflows`）

- [ ] **Step 1: 写失败测试**

  `tests/test_admin_web.py` 顶部补 `import json`（该文件当前没有）。

  追加到 `tests/test_admin_web.py`：

  ```python
  async def test_admin_workflow_editor_page_renders_canvas():
      """编排页返回画布容器，且不下发任何 client secret 之类的敏感值。"""
      from app.models.workflow import Workflow

      client, _, _, _ = await _create_seeded_logged_in_super_admin()
      tag = uuid.uuid4().hex[:8]
      code = f"wf-{tag}"
      async with AdminSessionLocal() as db:
          db.add(
              Workflow(
                  code=code,
                  name="Canvas Flow",
                  description="",
                  definition={
                      "entry": "step1",
                      "nodes": [
                          {
                              "id": "step1",
                              "type": "agent",
                              "label": "意图识别",
                              "agentCode": "planner",
                          }
                      ],
                      "edges": [],
                  },
                  enabled=1,
              )
          )
          await db.commit()
      try:
          response = await client.get(f"/admin/workflows/{code}")
          assert response.status_code == 200, response.text
          assert "workflow-editor-canvas" in response.text
          assert "workflow-editor-state" in response.text
          assert "意图识别" in response.text
          # 画布页不应出现 client secret / 密码一类的字样
          assert "client_secret" not in response.text
      finally:
          await client.aclose()


  async def test_admin_workflow_update_persists_layout_and_label():
      """保存后 layout 坐标与节点 label 必须原样落库。"""
      from app.models.workflow import Workflow

      client, _, _, csrf = await _create_seeded_logged_in_super_admin()
      tag = uuid.uuid4().hex[:8]
      code = f"wf-{tag}"
      async with AdminSessionLocal() as db:
          db.add(
              Workflow(
                  code=code,
                  name="Layout Flow",
                  description="",
                  definition={
                      "entry": "step1",
                      "nodes": [
                          {
                              "id": "step1",
                              "type": "agent",
                              "label": "意图识别",
                              "agentCode": "planner",
                          }
                      ],
                      "edges": [],
                  },
                  enabled=1,
              )
          )
          await db.commit()
      definition = {
          "entry": "step1",
          "inputTemplate": "问：{{user_input}}",
          "nodes": [
              {
                  "id": "step1",
                  "type": "agent",
                  "label": "意图识别",
                  "agentCode": "planner",
              }
          ],
          "edges": [],
          "layout": {
              "nodes": {"step1": {"x": 320, "y": 640}},
              "viewport": {"x": 12, "y": 34, "scale": 1.5},
          },
      }
      try:
          updated = await client.post(
              f"/admin/workflows/{code}/update",
              data={
                  "csrf_token": csrf,
                  "name": "Layout Flow",
                  "description": "",
                  "enabled": "1",
                  "definition": json.dumps(definition, ensure_ascii=False),
                  "next": f"/admin/workflows/{code}",
              },
          )
          assert updated.status_code == 303, updated.text
          assert updated.headers["location"] == f"/admin/workflows/{code}"
          async with AdminSessionLocal() as db:
              row = await db.scalar(
                  sa.select(Workflow).where(Workflow.code == code)
              )
              assert row is not None
              assert row.definition["layout"]["nodes"]["step1"] == {
                  "x": 320,
                  "y": 640,
              }
              assert row.definition["layout"]["viewport"]["scale"] == 1.5
              assert row.definition["nodes"][0]["label"] == "意图识别"
      finally:
          await client.aclose()


  async def test_admin_workflow_editor_page_missing_workflow_returns_404():
      """不存在的编排返回 404，不泄露存在性差异。"""
      client, _, _, _ = await _create_seeded_logged_in_super_admin()
      try:
          response = await client.get("/admin/workflows/does-not-exist")
          assert response.status_code == 404
      finally:
          await client.aclose()


  async def test_admin_workflow_create_accepts_agent_code_and_redirects_to_editor():
      """创建表单不再收 JSON，改收初始 Agent code。"""
      from app.models.agent import Agent

      client, _, _, csrf = await _create_seeded_logged_in_super_admin()
      tag = uuid.uuid4().hex[:8]
      agent_code = f"ag-{tag}"
      code = f"wf-{tag}"
      async with AdminSessionLocal() as db:
          db.add(
              Agent(
                  tenant_id="default",
                  code=agent_code,
                  name="Planner",
                  model="test-model",
                  system_prompt="p",
                  tools=[],
                  skills=[],
                  enabled=1,
              )
          )
          await db.commit()
      try:
          created = await client.post(
              "/admin/workflows",
              data={
                  "csrf_token": csrf,
                  "code": code,
                  "name": "New Flow",
                  "description": "",
                  "agent_code": agent_code,
                  "enabled": "1",
              },
          )
          assert created.status_code == 303, created.text
          assert created.headers["location"] == f"/admin/workflows/{code}"
          async with AdminSessionLocal() as db:
              from app.models.workflow import Workflow

              row = await db.scalar(
                  sa.select(Workflow).where(Workflow.code == code)
              )
              assert row is not None
              assert row.definition["nodes"][0]["agentCode"] == agent_code
              assert row.definition["nodes"][0]["id"] == "step1"
      finally:
          await client.aclose()
  ```

  > `Agent` 模型的必填字段以 `app/models/agent.py` 为准；构造前先确认列名，
  > 缺哪个补哪个（用 `sa.text` 或最小字段集均可，只要能通过 `enabled=1` 的
  > 列表过滤）。

- [ ] **Step 2: 跑测试确认失败**

  ```bash
  python -m pytest tests/test_admin_web.py -q -k "workflow_editor or workflow_create"
  ```

  Expected: FAIL —— 编排页 404、创建仍要求 `definition`。

- [ ] **Step 3: `shell()` 支持额外脚本**

  在 `services/admin_web/views.py` 的 `shell` 签名里加参数：

  ```python
  def shell(
      *,
      title_text: str,
      principal: AdminPrincipal,
      csrf: str,
      theme: str,
      color_mode: str,
      density: str,
      pages: Iterable[AdminPageDefinition],
      content: Any,
      flash: str | None = None,
      flash_kind: str = "info",
      extra_scripts: list[str] | None = None,
  ) -> Any:
  ```

  并把 `head(...)` 里的脚本部分改成：

  ```python
            link(rel="stylesheet", href=ADMIN_STYLESHEET),
            script(src=ADMIN_UI_SCRIPT),
            *(script(src=src) for src in (extra_scripts or [])),
  ```

- [ ] **Step 4: 新增 `workflow_editor_page`**

  先把 `h3` 和 `pre` 加进 `services/admin_web/views.py` 顶部的 `from ludic.html import (...)`：

  ```python
      h3,
      ...
      pre,
  ```

  （已核实：`ludic.html` 提供 `h3` 与 `pre`；`option` 支持 `selected`，
  `div` 支持 `hidden`，`textarea` 支持 `rows` / `readonly`，
  任意 `data-*` 通过 `attrs={"data-node-type": "..."}` 传入。）

  在 `views.py` 的 `workflows_page` 之后插入：

  ```python
  def workflow_editor_page(
      *,
      principal: AdminPrincipal,
      csrf: str,
      pages: Iterable[AdminPageDefinition],
      workflow: Any,
      agents: list[Any],
      theme: str,
      color_mode: str,
      density: str,
      can_write: bool,
      flash: str | None = None,
  ) -> Any:
      """渲染 Workflow 画布编排页。"""
      import json as _json

      state = {
          "code": workflow.code,
          "name": workflow.name,
          "description": workflow.description or "",
          "version": int(workflow.version or 1),
          "enabled": bool(workflow.enabled),
          "canWrite": bool(can_write),
          "definition": workflow.definition or {},
          "agents": [
              {"code": a.code, "name": a.name, "enabled": bool(a.enabled)}
              for a in agents
          ],
      }
      state_text = _json.dumps(state, ensure_ascii=False)
      palette = div(
          h2("Node palette"),
          div(
              button(
                  "Agent node",
                  type="button",
                  class_="btn btn-secondary workflow-palette-item",
                  attrs={"data-node-type": "agent"},
              ),
              button(
                  "Approval node",
                  type="button",
                  class_="btn btn-secondary workflow-palette-item",
                  attrs={"data-node-type": "approval"},
              ),
              class_="workflow-palette",
          ),
          h3("Available agents"),
          ul(
              *(
                  li(
                      span(f"{a.name} "),
                      small(a.code, class_="block-muted"),
                      class_="workflow-agent-item",
                  )
                  for a in agents
              )
          ),
          class_="surface workflow-palette-panel",
      )
      canvas = div(
          div(id="workflow-editor-canvas", class_="workflow-editor-canvas"),
          class_="workflow-editor-canvas-wrap",
      )
      inspector = div(
          h2("Inspector"),
          div(id="workflow-editor-inspector", class_="workflow-editor-inspector"),
          class_="surface workflow-editor-inspector-panel",
      )
      toolbar = div(
          a("Back to list", href="/admin/workflows", class_="btn btn-secondary"),
          h1(workflow.name),
          small(workflow.code, class_="block-muted"),
          span(
              "Enabled" if workflow.enabled else "Disabled",
              class_="status-badge",
          ),
          div(
              button(
                  "Undo",
                  type="button",
                  class_="btn btn-secondary btn-small",
                  id="workflow-editor-undo",
                  disabled=not can_write,
              ),
              button(
                  "Redo",
                  type="button",
                  class_="btn btn-secondary btn-small",
                  id="workflow-editor-redo",
                  disabled=not can_write,
              ),
              button(
                  "Auto layout",
                  type="button",
                  class_="btn btn-secondary btn-small",
                  id="workflow-editor-layout",
                  disabled=not can_write,
              ),
              button(
                  "JSON",
                  type="button",
                  class_="btn btn-secondary btn-small",
                  id="workflow-editor-json-toggle",
              ),
              class_="workflow-editor-toolbar-actions",
          ),
          class_="workflow-editor-toolbar",
      )
      save_form = (
          form(
              _csrf(csrf),
              label(span("Name"), input_(name="name", value=workflow.name, required=True)),
              label(
                  span("Description"),
                  input_(name="description", value=workflow.description or ""),
              ),
              label(
                  span("Enabled"),
                  select(
                      option("Yes", value="1", selected=bool(workflow.enabled)),
                      option("No", value="0", selected=not bool(workflow.enabled)),
                      name="enabled",
                  ),
              ),
              input_(
                  type="hidden",
                  name="definition",
                  id="workflow-editor-definition",
                  value=_json.dumps(
                      workflow.definition or {}, ensure_ascii=False
                  ),
              ),
              input_(type="hidden", name="next", value=f"/admin/workflows/{workflow.code}"),
              button(
                  "Save workflow",
                  type="submit",
                  class_="btn btn-primary",
                  disabled=not can_write,
              ),
              action=f"/admin/workflows/{workflow.code}/update",
              method="post",
              class_="workflow-editor-save-form",
          )
          if can_write
          else div(
              span("Read only", class_="status-badge"),
              class_="workflow-editor-save-form",
          )
      )
      state_block = textarea(
          state_text,
          id="workflow-editor-state",
          name="workflow_editor_state",
          rows=1,
          readonly=True,
          class_="workflow-editor-state",
      )
      json_view = div(
          pre(
              _json.dumps(workflow.definition or {}, ensure_ascii=False, indent=2),
              id="workflow-editor-json",
              class_="workflow-editor-json",
          ),
          class_="surface workflow-editor-json-panel",
          id="workflow-editor-json-panel",
          hidden=True,
      )
      error_bar = div(
          ul(id="workflow-editor-errors", class_="workflow-editor-errors"),
          class_="workflow-editor-errorbar",
          id="workflow-editor-errorbar",
          hidden=True,
      )
      return shell(
          title_text=f"{workflow.name} · Workflow editor",
          principal=principal,
          csrf=csrf,
          theme=theme,
          color_mode=color_mode,
          density=density,
          pages=pages,
          flash=flash,
          content=div(
              section(toolbar, class_="surface"),
              div(
                  palette,
                  canvas,
                  inspector,
                  class_="workflow-editor-layout",
              ),
              error_bar,
              json_view,
              state_block,
              save_form,
              class_="stack",
          ),
          extra_scripts=[
              "/admin/static/workflow-editor.css",
              "/admin/static/workflow-editor-core.js",
              "/admin/static/workflow-editor.js",
          ],
      )
  ```

  > `hidden=True` 已被 Ludic 渲染成 `hidden="hidden"`（已核实），无需 CSS 兜底。

- [ ] **Step 5: 列表页改造**

  把 `workflows_page` 里的每行编辑表单换成链接：

  ```python
          editor = (
              a(
                  "Open editor",
                  href=f"/admin/workflows/{row.code}",
                  class_="btn btn-secondary btn-small",
              )
              if can_write
              else span("Read only", class_="status-badge")
          )
  ```

  创建表单去掉 JSON 文本框，改成「初始 Agent」下拉：

  ```python
          label(
              span("Initial agent"),
              select(
                  *(
                      option(f"{a.name} ({a.code})", value=a.code)
                      for a in agents
                  ),
                  name="agent_code",
                  required=True,
              ),
          ),
  ```

  并给 `workflows_page` 加参数 `agents: list[Any]`，把 `agents` 传进来。

- [ ] **Step 6: 路由与创建端点**

  在 `app.py` 的 `workflows_get` 之后插入：

  ```python
  @app.get("/admin/workflows/{code}")
  async def workflow_editor_get(code: str, request: Request):
      """渲染 Workflow 画布编排页。"""
      values = await _with_auth(request)
      if values is None:
          return _unauthorized()
      db, principal, pref, csrf = values
      try:
          if not await can_access_page(db, principal, "workflows"):
              return _not_found()
          row = await workflow_repo.get_by_code(db, code)
          if row is None:
              return _not_found()
          agents = await agent_repo.list(
              db, only_enabled=True, limit=500, tenant_id=_tenant_id(principal)
          )
          return workflow_editor_page(
              principal=principal,
              csrf=csrf,
              pages=await visible_pages(db, principal),
              workflow=row,
              agents=agents,
              theme=pref.theme,
              color_mode=pref.color_mode,
              density=pref.density,
              can_write=await _can_edit_resource(
                  db,
                  principal,
                  page_code="workflows",
                  component_code="workflow_editor",
              ),
          )
      finally:
          await db.close()
  ```

  > 路由顺序：`@app.get("/admin/workflows/{code}")` 必须放在
  > `@app.get("/admin/workflows")` **之后**（Starlette 按注册顺序匹配，
  > 两者路径不冲突，但保持「具体在前」的惯例）。`POST /admin/workflows/validate`
  > 是 POST，不与 GET 冲突。

  把 `workflows_get` 改成同时取 agents 并传给 `workflows_page`：

  ```python
              rows=await workflow_repo.list(db, limit=500),
              agents=await agent_repo.list(
                  db, only_enabled=True, limit=500, tenant_id=_tenant_id(principal)
              ),
  ```

  创建端点 `workflows_post` 改成按 `agent_code` 生成骨架：

  ```python
          agent_code = str(form.get("agent_code") or "").strip()
          if not agent_code:
              return _business_redirect("/admin/workflows", error="invalid")
          if (
              await agent_repo.get_by_code(
                  db, agent_code, tenant_id=_tenant_id(principal)
              )
              is None
          ):
              # 初始 Agent 必须真实存在，否则建出来的编排一跑就找不到 Agent。
              return _business_redirect("/admin/workflows", error="invalid")
          try:
              payload = WorkflowCreate(
                  code=str(form.get("code") or ""),
                  name=str(form.get("name") or ""),
                  description=str(form.get("description") or ""),
                  definition=WorkflowDefinition(
                      entry="step1",
                      nodes=[
                          WorkflowNode(
                              id="step1",
                              type="agent",
                              agentCode=agent_code,
                          )
                      ],
                      edges=[],
                  ),
                  enabled=_as_bool(form.get("enabled"), True),
              )
          except (ValidationError, ValueError):
              return _business_redirect("/admin/workflows", error="invalid")
  ```

  并把创建成功后的重定向改成跳编排页：

  ```python
          return RedirectResponse(f"/admin/workflows/{row.code}", status_code=303)
  ```

  > `WorkflowCreate.definition` 的类型是 `WorkflowDefinition`，可以直接传模型
  > 实例。需要在 `app.py` 顶部补 `from app.core.workflow_def import (
  > WorkflowDefinition, WorkflowNode, collect_graph_issues )`。

  更新端点 `workflows_update` 支持 `next`：

  ```python
          next_url = _safe_next(str(form.get("next") or "/admin/workflows"))
          ...
          return RedirectResponse(next_url, status_code=303)
  ```

  > `_safe_next` 已存在，只接受 `/admin/` 内部路径，无开放重定向风险。

- [ ] **Step 7: 跑测试确认通过**

  ```bash
  python -m pytest tests/test_admin_web.py -q
  ```

  Expected: PASS。

- [ ] **Step 8: 提交**

  ```bash
  git add services/admin_web/views.py services/admin_web/app.py tests/test_admin_web.py
  git commit -m "feat: add workflow canvas editor page"
  ```

---

## Task 5: `workflow-editor-core.js` 纯逻辑

**Files:**
- Create: `services/admin_web/static/workflow-editor-core.js`
- Create: `tests/js/workflow-editor-core.test.mjs`

**Interfaces:**
- Consumes: 无
- Produces（挂到 `globalThis.WorkflowEditorCore`）：

  ```js
  {
    NODE_WIDTH, NODE_HEIGHT, GRID_SIZE,
    toModel(definition) -> Model,
    toDefinition(model) -> object,
    createUndoStack(limit) -> { push, undo, redo, canUndo, canRedo, reset, size },
    computeLayout(model) -> { nodes: {id: {x, y}}, viewport: {x, y, scale} },
    nextNodeId(model, type) -> string,
    connectNodes(model, fromId, toId) -> { ok: boolean, reason?: string },
    removeNode(model, nodeId) -> Model,
    setNodeType(model, nodeId, type) -> Model,
    cloneModel(model) -> Model,
  }
  ```

  `Model` 结构：

  ```js
  {
    entry: string,
    inputTemplate: string,
    nodes: [{ id, type, label, agentCode, outputKey, prompt,
              requiredApprovals, approvalTimeoutSec, approvalTimeoutAction }],
    edges: [{ from, to, when }],
    layout: { nodes: { [id]: { x, y } }, viewport: { x, y, scale } },
  }
  ```

- [ ] **Step 1: 写失败测试**

  创建 `tests/js/workflow-editor-core.test.mjs`：

  ```js
  import { readFile } from "node:fs/promises";
  import test from "node:test";
  import assert from "node:assert/strict";

  const CORE_PATH = new URL(
    "../../services/admin_web/static/workflow-editor-core.js",
    import.meta.url,
  );

  async function loadCore() {
    const source = await readFile(CORE_PATH, "utf8");
    // 用 new Function 在**当前 realm** 求值，而不是 node:vm 沙箱：
    // vm 里造出来的对象带着另一个 realm 的 Object.prototype，
    // assert/strict 的 deepStrictEqual 会以「结构相同但原型不同」失败。
    // new Function 不引入沙箱，原型与测试文件一致，deepEqual 才能用。
    return new Function(`${source}
  return globalThis.WorkflowEditorCore;`)();
  }

  const core = await loadCore();

  test("toModel 读取 nodes/edges/entry 与 layout 坐标", () => {
    const model = core.toModel({
      entry: "step1",
      inputTemplate: "问：{{user_input}}",
      nodes: [
        { id: "step1", type: "agent", label: "识别", agentCode: "planner" },
        { id: "rev", type: "approval", prompt: "确认" },
      ],
      edges: [{ from: "step1", to: "rev", when: 'upstream.a == "x"' }],
      layout: { nodes: { step1: { x: 80, y: 120 } } },
    });
    assert.equal(model.entry, "step1");
    assert.equal(model.nodes.length, 2);
    assert.equal(model.nodes[0].label, "识别");
    assert.equal(model.nodes[1].type, "approval");
    assert.equal(model.edges[0].when, 'upstream.a == "x"');
    assert.deepEqual(model.layout.nodes.step1, { x: 80, y: 120 });
  });

  test("toDefinition 往返后与原始 definition 等价", () => {
    const definition = {
      entry: "a",
      inputTemplate: "问：{{user_input}}",
      nodes: [{ id: "a", type: "agent", label: "起手", agentCode: "p" }],
      edges: [],
      layout: { nodes: { a: { x: 10, y: 20 } }, viewport: { x: 0, y: 0, scale: 1 } },
    };
    const model = core.toModel(definition);
    const back = core.toDefinition(model);
    assert.equal(back.entry, "a");
    assert.equal(back.nodes[0].label, "起手");
    assert.deepEqual(back.layout.nodes.a, { x: 10, y: 20 });
  });

  test("toDefinition 不产生 null 字段", () => {
    const model = core.toModel({
      nodes: [{ id: "a", type: "agent", agentCode: "p" }],
      edges: [],
    });
    const back = core.toDefinition(model);
    assert.equal("label" in back.nodes[0], true);
    assert.equal(back.nodes[0].label, "");
    assert.equal(JSON.stringify(back).includes("null"), false);
  });

  test("nextNodeId 生成不冲突的 stepN", () => {
    const model = core.toModel({
      nodes: [{ id: "step1", type: "agent", agentCode: "p" }],
      edges: [],
    });
    assert.equal(core.nextNodeId(model, "agent"), "step2");
    assert.equal(core.nextNodeId(model, "approval"), "approval1");
  });

  test("connectNodes 拒绝自环与重复边", () => {
    const model = core.toModel({
      nodes: [
        { id: "a", type: "agent", agentCode: "p" },
        { id: "b", type: "agent", agentCode: "p" },
      ],
      edges: [{ from: "a", to: "b" }],
    });
    assert.equal(core.connectNodes(model, "a", "a").ok, false);
    assert.equal(core.connectNodes(model, "a", "b").ok, false);
    assert.equal(core.connectNodes(model, "b", "a").ok, true);
    assert.equal(model.edges.length, 2);
  });

  test("removeNode 同时删掉挂在它上面的边", () => {
    const model = core.toModel({
      nodes: [
        { id: "a", type: "agent", agentCode: "p" },
        { id: "b", type: "agent", agentCode: "p" },
      ],
      edges: [{ from: "a", to: "b" }],
    });
    const next = core.removeNode(model, "a");
    assert.equal(next.nodes.length, 1);
    assert.equal(next.edges.length, 0);
  });

  test("setNodeType 切换时清掉互斥字段", () => {
    const model = core.toModel({
      nodes: [{ id: "a", type: "agent", agentCode: "p" }],
      edges: [],
    });
    const asApproval = core.setNodeType(model, "a", "approval");
    assert.equal(asApproval.nodes[0].agentCode, "");
    assert.equal(asApproval.nodes[0].prompt, "");
    const back = core.setNodeType(asApproval, "a", "agent");
    assert.equal(back.nodes[0].prompt, "");
    assert.equal(back.nodes[0].requiredApprovals, 1);
  });

  test("computeLayout 按最长路径分层", () => {
    const model = core.toModel({
      entry: "a",
      nodes: [
        { id: "a", type: "agent", agentCode: "p" },
        { id: "b", type: "agent", agentCode: "p" },
        { id: "c", type: "agent", agentCode: "p" },
        { id: "d", type: "agent", agentCode: "p" },
      ],
      edges: [
        { from: "a", to: "b" },
        { from: "a", to: "c" },
        { from: "b", to: "d" },
        { from: "c", to: "d" },
      ],
    });
    const layout = core.computeLayout(model);
    // a 是第 0 层，b/c 是第 1 层，d 是第 2 层
    assert.ok(layout.nodes.b.x > layout.nodes.a.x);
    assert.equal(layout.nodes.b.x, layout.nodes.c.x);
    assert.ok(layout.nodes.d.x > layout.nodes.b.x);
    assert.notEqual(layout.nodes.b.y, layout.nodes.c.y);
  });

  test("computeLayout 对同一输入产出同一结果", () => {
    const model = core.toModel({
      entry: "a",
      nodes: [
        { id: "a", type: "agent", agentCode: "p" },
        { id: "b", type: "agent", agentCode: "p" },
      ],
      edges: [{ from: "a", to: "b" }],
    });
    assert.deepEqual(core.computeLayout(model), core.computeLayout(model));
  });

  test("撤销栈能回到上一步并可重做", () => {
    const stack = core.createUndoStack(50);
    const state = core.toModel({ nodes: [{ id: "a", type: "agent", agentCode: "p" }], edges: [] });
    stack.reset(state);
    const changed = core.cloneModel(state);
    changed.nodes.push({ id: "b", type: "agent", agentCode: "p" });
    stack.push(changed);
    assert.equal(stack.canUndo(), true);
    const back = stack.undo();
    assert.equal(back.nodes.length, 1);
    assert.equal(stack.canRedo(), true);
    const again = stack.redo();
    assert.equal(again.nodes.length, 2);
  });

  test("撤销栈超过上限后丢弃最旧的记录", () => {
    const stack = core.createUndoStack(3);
    const base = core.toModel({ nodes: [{ id: "a", type: "agent", agentCode: "p" }], edges: [] });
    stack.reset(base);
    for (let i = 0; i < 10; i += 1) {
      const next = core.cloneModel(stack.current());
      next.nodes.push({ id: `n${i}`, type: "agent", agentCode: "p" });
      stack.push(next);
    }
    assert.equal(stack.size(), 3);
  });
  ```

- [ ] **Step 2: 跑测试确认失败**

  ```bash
  node --test tests/js/workflow-editor-core.test.mjs
  ```

  Expected: FAIL —— 文件不存在（ENOENT）。

- [ ] **Step 3: 实现 core.js**

  创建 `services/admin_web/static/workflow-editor-core.js`：

  ```javascript
  /**
   * Workflow 画布的纯逻辑层。
   *
   * 这里**不许**出现 document / window / DOMParser：所有函数必须能在
   * node:vm 里裸跑，测试文件 tests/js/workflow-editor-core.test.mjs
   * 依赖这一点。
   *
   * 画布不持有第二份状态：definition ⇄ model 的转换是唯一的真相来源。
   */
  (function () {
    "use strict";

    const NODE_WIDTH = 200;
    const NODE_HEIGHT = 72;
    const GRID_SIZE = 8;
    const LAYOUT_GAP_X = 96;
    const LAYOUT_GAP_Y = 48;
    const LAYOUT_MARGIN = 40;

    function clone(value) {
      return JSON.parse(JSON.stringify(value));
    }

    function normalizeNode(raw) {
      const type = raw && raw.type === "approval" ? "approval" : "agent";
      return {
        id: String((raw && raw.id) || ""),
        type,
        label: String((raw && raw.label) || ""),
        agentCode: String((raw && raw.agentCode) || ""),
        outputKey: String((raw && raw.outputKey) || ""),
        prompt: String((raw && raw.prompt) || ""),
        requiredApprovals: Number((raw && raw.requiredApprovals) || 1),
        approvalTimeoutSec: Number((raw && raw.approvalTimeoutSec) || 0),
        approvalTimeoutAction:
          String((raw && raw.approvalTimeoutAction) || "reject"),
      };
    }

    function normalizeEdge(raw) {
      return {
        from: String((raw && raw.from) || ""),
        to: String((raw && raw.to) || ""),
        when: String((raw && raw.when) || ""),
      };
    }

    function toModel(definition) {
      const def = definition || {};
      const nodes = (def.nodes || []).map(normalizeNode);
      const edges = (def.edges || []).map(normalizeEdge);
      const layout = def.layout || {};
      const layoutNodes = {};
      Object.keys(layout.nodes || {}).forEach((id) => {
        const point = layout.nodes[id] || {};
        layoutNodes[id] = {
          x: Number(point.x || 0),
          y: Number(point.y || 0),
        };
      });
      const viewport = layout.viewport || {};
      return {
        entry: String(def.entry || (nodes[0] ? nodes[0].id : "")),
        inputTemplate: String(
          def.inputTemplate || "用户问题：{{user_input}}\n上游产出：{{upstream}}",
        ),
        nodes,
        edges,
        layout: {
          nodes: layoutNodes,
          viewport: {
            x: Number(viewport.x || 0),
            y: Number(viewport.y || 0),
            scale: Number(viewport.scale || 1),
          },
        },
      };
    }

    function toDefinition(model) {
      return {
        entry: model.entry || "",
        inputTemplate: model.inputTemplate || "",
        nodes: model.nodes.map((node) => ({
          id: node.id,
          type: node.type,
          label: node.label || "",
          agentCode: node.agentCode || "",
          outputKey: node.outputKey || "",
          prompt: node.prompt || "",
          requiredApprovals: Number(node.requiredApprovals || 1),
          approvalTimeoutSec: Number(node.approvalTimeoutSec || 0),
          approvalTimeoutAction: node.approvalTimeoutAction || "reject",
        })),
        edges: model.edges.map((edge) => ({
          from: edge.from,
          to: edge.to,
          when: edge.when || "",
        })),
        layout: {
          nodes: model.layout.nodes,
          viewport: model.layout.viewport,
        },
      };
    }

    function cloneModel(model) {
      return clone(model);
    }

    function nextNodeId(model, type) {
      const prefix = type === "approval" ? "approval" : "step";
      const used = new Set(model.nodes.map((node) => node.id));
      let index = 1;
      while (used.has(`${prefix}${index}`)) {
        index += 1;
      }
      return `${prefix}${index}`;
    }

    function connectNodes(model, fromId, toId) {
      if (fromId === toId) {
        return { ok: false, reason: "self_loop" };
      }
      const exists = model.edges.some(
        (edge) => edge.from === fromId && edge.to === toId,
      );
      if (exists) {
        return { ok: false, reason: "duplicate" };
      }
      const fromNode = model.nodes.find((node) => node.id === fromId);
      const toNode = model.nodes.find((node) => node.id === toId);
      if (!fromNode || !toNode) {
        return { ok: false, reason: "unknown_node" };
      }
      model.edges.push({ from: fromId, to: toId, when: "" });
      return { ok: true };
    }

    function removeNode(model, nodeId) {
      const next = clone(model);
      next.nodes = next.nodes.filter((node) => node.id !== nodeId);
      next.edges = next.edges.filter(
        (edge) => edge.from !== nodeId && edge.to !== nodeId,
      );
      if (next.entry === nodeId) {
        next.entry = next.nodes[0] ? next.nodes[0].id : "";
      }
      delete next.layout.nodes[nodeId];
      return next;
    }

    function setNodeType(model, nodeId, type) {
      const next = clone(model);
      const node = next.nodes.find((item) => item.id === nodeId);
      if (!node) {
        return next;
      }
      const target = type === "approval" ? "approval" : "agent";
      if (node.type === target) {
        return next;
      }
      node.type = target;
      if (target === "approval") {
        node.agentCode = "";
      } else {
        node.prompt = "";
        node.requiredApprovals = 1;
        node.approvalTimeoutSec = 0;
        node.approvalTimeoutAction = "reject";
      }
      return next;
    }

    function computeLayout(model) {
      const ids = model.nodes.map((node) => node.id);
      const idSet = new Set(ids);
      const predecessors = {};
      ids.forEach((id) => {
        predecessors[id] = [];
      });
      model.edges.forEach((edge) => {
        if (idSet.has(edge.from) && idSet.has(edge.to)) {
          predecessors[edge.to].push(edge.from);
        }
      });
      const level = {};
      ids.forEach((id) => {
        level[id] = 0;
      });
      // 最长路径分层：反复松弛，直到不再变化。图有环时收敛不到稳定值，
      // 但那种图本来就保存不了，这里取最后一次松弛的结果即可。
      for (let pass = 0; pass < ids.length; pass += 1) {
        let changed = false;
        ids.forEach((id) => {
          predecessors[id].forEach((prev) => {
            if (level[prev] + 1 > level[id]) {
              level[id] = level[prev] + 1;
              changed = true;
            }
          });
        });
        if (!changed) {
          break;
        }
      }
      const buckets = {};
      ids.forEach((id) => {
        const key = String(level[id]);
        if (!buckets[key]) {
          buckets[key] = [];
        }
        buckets[key].push(id);
      });
      const levels = Object.keys(buckets)
        .map(Number)
        .sort((left, right) => left - right);
      const positions = {};
      levels.forEach((depth, column) => {
        const bucket = buckets[String(depth)];
        bucket.forEach((id, row) => {
          positions[id] = {
            x: LAYOUT_MARGIN + column * (NODE_WIDTH + LAYOUT_GAP_X),
            y:
              LAYOUT_MARGIN +
              row * (NODE_HEIGHT + LAYOUT_GAP_Y),
          };
        });
      });
      return {
        nodes: positions,
        viewport: { x: 0, y: 0, scale: 1 },
      };
    }

    function createUndoStack(limit) {
      const max = Number(limit) > 0 ? Number(limit) : 50;
      let past = [];
      let future = [];
      let present = null;
      return {
        reset(state) {
          past = [];
          future = [];
          present = clone(state);
        },
        current() {
          return present ? clone(present) : null;
        },
        push(state) {
          if (!present) {
            present = clone(state);
            return;
          }
          past.push(present);
          if (past.length > max) {
            past = past.slice(past.length - max);
          }
          present = clone(state);
          future = [];
        },
        undo() {
          if (!past.length) {
            return present ? clone(present) : null;
          }
          future.unshift(present);
          present = past.pop();
          return clone(present);
        },
        redo() {
          if (!future.length) {
            return present ? clone(present) : null;
          }
          past.push(present);
          present = future.shift();
          return clone(present);
        },
        canUndo() {
          return past.length > 0;
        },
        canRedo() {
          return future.length > 0;
        },
        size() {
          return past.length;
        },
      };
    }

    const api = {
      NODE_WIDTH,
      NODE_HEIGHT,
      GRID_SIZE,
      toModel,
      toDefinition,
      cloneModel,
      nextNodeId,
      connectNodes,
      removeNode,
      setNodeType,
      computeLayout,
      createUndoStack,
    };

    if (typeof globalThis !== "undefined") {
      globalThis.WorkflowEditorCore = api;
    }
  })();
  ```

- [ ] **Step 4: 跑测试确认通过**

  ```bash
  node --test tests/js/workflow-editor-core.test.mjs
  ```

  Expected: 全部 PASS。

- [ ] **Step 5: 提交**

  ```bash
  git add services/admin_web/static/workflow-editor-core.js tests/js/workflow-editor-core.test.mjs
  git commit -m "feat: add workflow editor core logic"
  ```

---

## Task 6: 画布渲染骨架与节点拖拽

**Files:**
- Create: `services/admin_web/static/workflow-editor.js`
- Create: `services/admin_web/static/workflow-editor.css`
- Test: 浏览器手工验证（见 Step 6）

**Interfaces:**
- Consumes: Task 5 的 `globalThis.WorkflowEditorCore`；Task 4 页面里的
  `#workflow-editor-state`（textarea，内容是 JSON）、`#workflow-editor-canvas`、
  `#workflow-editor-inspector`、`#workflow-editor-errors`、`#workflow-editor-definition`
- Produces: 页面里可用的全局 `window.WorkflowEditor`（内部使用，不对外承诺 API）

- [ ] **Step 1: 创建 CSS**

  创建 `services/admin_web/static/workflow-editor.css`：

  ```css
  /* Workflow 画布编排器。颜色一律走 admin.css 已有的 CSS 变量，
     这样三套主题与日夜模式自动跟随，不另起一套调色板。 */

  .workflow-editor-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .workflow-editor-toolbar h1 {
    font-size: 18px;
    margin: 0;
  }

  .workflow-editor-toolbar-actions {
    display: flex;
    gap: 8px;
    margin-left: auto;
  }

  .workflow-editor-layout {
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr) 320px;
    gap: 16px;
    align-items: start;
  }

  .workflow-editor-canvas-wrap {
    min-height: 620px;
    background: var(--color-bg-subtle);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-surface);
    overflow: hidden;
  }

  .workflow-editor-canvas {
    position: relative;
    height: 620px;
    cursor: grab;
    background-image:
      linear-gradient(to right, var(--color-border) 1px, transparent 1px),
      linear-gradient(to bottom, var(--color-border) 1px, transparent 1px);
    background-size: 24px 24px;
    touch-action: none;
  }

  .workflow-editor-canvas.is-panning {
    cursor: grabbing;
  }

  .workflow-editor-canvas svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }

  .workflow-node {
    cursor: move;
  }

  .workflow-node rect.body {
    fill: var(--color-bg-surface);
    stroke: var(--color-border);
    stroke-width: 1;
    rx: 8;
  }

  .workflow-node.is-selected rect.body {
    stroke: var(--color-accent);
    stroke-width: 2;
  }

  .workflow-node.is-error rect.body {
    stroke: var(--color-danger);
    stroke-width: 2;
  }

  .workflow-node text.title {
    fill: var(--color-text-primary);
    font-size: 13px;
    font-weight: 600;
  }

  .workflow-node text.subtitle {
    fill: var(--color-text-secondary);
    font-size: 11px;
  }

  .workflow-node text.badge {
    fill: var(--color-accent-text);
    font-size: 10px;
  }

  .workflow-node rect.badge-bg {
    fill: var(--color-accent);
    rx: 4;
  }

  .workflow-node circle.port {
    fill: var(--color-bg-surface);
    stroke: var(--color-accent);
    stroke-width: 2;
    cursor: crosshair;
  }

  .workflow-edge path {
    fill: none;
    stroke: var(--color-text-secondary);
    stroke-width: 2;
  }

  .workflow-edge.is-conditional path {
    stroke: var(--color-warning);
    stroke-dasharray: 6 4;
  }

  .workflow-edge.is-selected path {
    stroke: var(--color-accent);
    stroke-width: 3;
  }

  .workflow-edge.is-error path {
    stroke: var(--color-danger);
    stroke-width: 3;
  }

  .workflow-edge text {
    fill: var(--color-text-secondary);
    font-size: 10px;
  }

  .workflow-editor-palette-panel,
  .workflow-editor-inspector-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .workflow-palette {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .workflow-editor-inspector {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .workflow-editor-inspector label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
  }

  .workflow-editor-inspector input,
  .workflow-editor-inspector select,
  .workflow-editor-inspector textarea {
    width: 100%;
    box-sizing: border-box;
  }

  .workflow-editor-errorbar {
    border: 1px solid var(--color-danger);
    border-radius: var(--radius-surface);
    padding: 12px 16px;
    background: var(--color-bg-surface);
  }

  .workflow-editor-errors {
    margin: 0;
    padding-left: 20px;
    color: var(--color-danger);
    font-size: 13px;
  }

  .workflow-editor-json-panel {
    max-height: 320px;
    overflow: auto;
  }

  .workflow-editor-json {
    margin: 0;
    font-size: 12px;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .workflow-editor-state {
    display: none;
  }

  .workflow-editor-save-form {
    display: flex;
    align-items: flex-end;
    gap: 12px;
    flex-wrap: wrap;
  }

  .workflow-editor-save-form label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
  }

  @media (max-width: 1100px) {
    .workflow-editor-layout {
      grid-template-columns: 1fr;
    }
  }
  ```

- [ ] **Step 2: 实现 `workflow-editor.js` 的骨架**

  创建 `services/admin_web/static/workflow-editor.js`：

  ```javascript
  /**
   * Workflow 画布的 DOM/SVG 交互层。
   *
   * 只做三件事：把 model 画成 SVG、把用户操作翻译成 model 变更、
   * 把 model 变更同步回隐藏表单字段。所有业务判断都委托给
   * globalThis.WorkflowEditorCore。
   */
  (function () {
    "use strict";

    const core = globalThis.WorkflowEditorCore;
    const SVG_NS = "http://www.w3.org/2000/svg";

    const state = {
      model: null,
      stack: null,
      selection: null, // { kind: "node" | "edge", id: string }
      agents: [],
      canWrite: false,
      code: "",
      viewport: { x: 0, y: 0, scale: 1 },
      issues: [],
      dirty: false,
    };

    function el(tag, attrs, text) {
      const node = document.createElementNS(SVG_NS, tag);
      Object.keys(attrs || {}).forEach((key) => {
        node.setAttribute(key, String(attrs[key]));
      });
      if (text !== undefined && text !== null) {
        node.textContent = String(text);
      }
      return node;
    }

    function readState() {
      const holder = document.getElementById("workflow-editor-state");
      if (!holder) {
        return null;
      }
      try {
        return JSON.parse(holder.value || "{}");
      } catch (err) {
        return null;
      }
    }

    function init() {
      const payload = readState();
      if (!payload || !core) {
        return;
      }
      state.model = core.toModel(payload.definition || {});
      state.stack = core.createUndoStack(50);
      state.stack.reset(state.model);
      state.agents = payload.agents || [];
      state.canWrite = Boolean(payload.canWrite);
      state.code = payload.code || "";
      state.viewport = Object.assign(
        { x: 0, y: 0, scale: 1 },
        state.model.layout.viewport || {},
      );
      render();
    }

    function render() {
      const canvas = document.getElementById("workflow-editor-canvas");
      if (!canvas) {
        return;
      }
      canvas.innerHTML = "";
      const svg = el("svg", { width: "100%", height: "100%" });
      const root = el("g", {
        transform: `translate(${state.viewport.x}, ${state.viewport.y}) scale(${state.viewport.scale})`,
      });
      const edgeLayer = el("g", { class: "workflow-edges" });
      const nodeLayer = el("g", { class: "workflow-nodes" });
      root.appendChild(edgeLayer);
      root.appendChild(nodeLayer);
      svg.appendChild(root);
      canvas.appendChild(svg);
      renderEdges(edgeLayer);
      renderNodes(nodeLayer);
      syncDefinitionInput();
    }

    function nodePosition(nodeId) {
      const point = state.model.layout.nodes[nodeId];
      if (point) {
        return point;
      }
      const index = state.model.nodes.findIndex((node) => node.id === nodeId);
      return { x: 40 + index * 40, y: 40 };
    }

    function renderEdges(layer) {
      state.model.edges.forEach((edge, index) => {
        const from = nodePosition(edge.from);
        const to = nodePosition(edge.to);
        const x1 = from.x + core.NODE_WIDTH;
        const y1 = from.y + core.NODE_HEIGHT / 2;
        const x2 = to.x;
        const y2 = to.y + core.NODE_HEIGHT / 2;
        const group = el("g", { class: "workflow-edge" });
        if (edge.when) {
          group.classList.add("is-conditional");
        }
        if (
          state.selection &&
          state.selection.kind === "edge" &&
          state.selection.id === edgeKey(index)
        ) {
          group.classList.add("is-selected");
        }
        const path = el("path", {
          d: `M ${x1} ${y1} C ${x1 + 60} ${y1}, ${x2 - 60} ${y2}, ${x2} ${y2}`,
        });
        group.appendChild(path);
        if (edge.when) {
          group.appendChild(
            el("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 6 }, edge.when),
          );
        }
        group.addEventListener("click", () => {
          select("edge", edgeKey(index));
        });
        layer.appendChild(group);
      });
    }

    function edgeKey(index) {
      return `edge-${index}`;
    }

    function renderNodes(layer) {
      state.model.nodes.forEach((node) => {
        const position = nodePosition(node.id);
        const group = el("g", {
          class: "workflow-node",
          transform: `translate(${position.x}, ${position.y})`,
          "data-node-id": node.id,
        });
        if (
          state.selection &&
          state.selection.kind === "node" &&
          state.selection.id === node.id
        ) {
          group.classList.add("is-selected");
        }
        if (state.issues.some((issue) => issue.node === node.id)) {
          group.classList.add("is-error");
        }
        group.appendChild(
          el("rect", {
            class: "body",
            width: core.NODE_WIDTH,
            height: core.NODE_HEIGHT,
          }),
        );
        group.appendChild(
          el("rect", { class: "badge-bg", width: 52, height: 16, x: 12, y: 10 }),
        );
        group.appendChild(
          el(
            "text",
            { class: "badge", x: 18, y: 22 },
            node.type === "approval" ? "APPROVAL" : "AGENT",
          ),
        );
        group.appendChild(
          el(
            "text",
            { class: "title", x: 12, y: 44 },
            node.label || node.outputKey || node.id,
          ),
        );
        group.appendChild(
          el("text", { class: "subtitle", x: 12, y: 60 }, nodeSubtitle(node)),
        );
        const input = el("circle", {
          class: "port port-in",
          cx: 0,
          cy: core.NODE_HEIGHT / 2,
          r: 6,
        });
        const output = el("circle", {
          class: "port port-out",
          cx: core.NODE_WIDTH,
          cy: core.NODE_HEIGHT / 2,
          r: 6,
        });
        group.appendChild(input);
        group.appendChild(output);
        layer.appendChild(group);
      });
    }

    function nodeSubtitle(node) {
      if (node.type === "approval") {
        return node.prompt ? node.prompt.split("\n")[0] : "（未填写审批说明）";
      }
      const agent = state.agents.find((item) => item.code === node.agentCode);
      return agent ? agent.name : node.agentCode || "（未选择 Agent）";
    }

    function syncDefinitionInput() {
      const input = document.getElementById("workflow-editor-definition");
      if (input) {
        input.value = JSON.stringify(core.toDefinition(state.model));
      }
    }

    function select(kind, id) {
      state.selection = kind ? { kind, id } : null;
      render();
    }

    function commit(nextModel) {
      state.stack.push(nextModel);
      state.model = nextModel;
      state.dirty = true;
      render();
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  })();
  ```

- [ ] **Step 3: 拖拽节点**

  在 `workflow-editor.js` 的 `renderNodes` 里，给节点 group 加上 pointer 事件，
  并在 `init()` 之后绑定画布的平移与缩放：

  ```javascript
    function bindCanvasInteractions(canvas) {
      let drag = null;

      canvas.addEventListener("pointerdown", (event) => {
        const nodeGroup = event.target.closest(".workflow-node");
        if (!nodeGroup || !state.canWrite) {
          return;
        }
        const nodeId = nodeGroup.getAttribute("data-node-id");
        const position = nodePosition(nodeId);
        drag = {
          kind: "node",
          nodeId,
          offsetX: (event.clientX - canvas.getBoundingClientRect().left - state.viewport.x) / state.viewport.scale - position.x,
          offsetY: (event.clientY - canvas.getBoundingClientRect().top - state.viewport.y) / state.viewport.scale - position.y,
        };
        canvas.setPointerCapture(event.pointerId);
        select("node", nodeId);
        event.preventDefault();
      });

      canvas.addEventListener("pointermove", (event) => {
        if (!drag || drag.kind !== "node") {
          return;
        }
        const rect = canvas.getBoundingClientRect();
        const x =
          (event.clientX - rect.left - state.viewport.x) / state.viewport.scale -
          drag.offsetX;
        const y =
          (event.clientY - rect.top - state.viewport.y) / state.viewport.scale -
          drag.offsetY;
        state.model.layout.nodes[drag.nodeId] = {
          x: Math.round(x / core.GRID_SIZE) * core.GRID_SIZE,
          y: Math.round(y / core.GRID_SIZE) * core.GRID_SIZE,
        };
        render();
      });

      canvas.addEventListener("pointerup", () => {
        if (drag && drag.kind === "node") {
          // 拖动结束时记一步撤销：拖动过程里不记。
          state.stack.push(state.model);
          state.dirty = true;
          syncDefinitionInput();
        }
        drag = null;
      });

      canvas.addEventListener(
        "wheel",
        (event) => {
          event.preventDefault();
          const factor = event.deltaY < 0 ? 1.1 : 0.9;
          const next = Math.min(2, Math.max(0.5, state.viewport.scale * factor));
          state.viewport.scale = next;
          render();
        },
        { passive: false },
      );
    }
  ```

  并在 `init()` 末尾调用 `bindCanvasInteractions(document.getElementById("workflow-editor-canvas"))`。

- [ ] **Step 4: 跑一遍语法检查**

  ```bash
  node --check services/admin_web/static/workflow-editor.js
  node --check services/admin_web/static/workflow-editor-core.js
  ```

  Expected: 无输出（通过）。

- [ ] **Step 5: 提交**

  ```bash
  git add services/admin_web/static/workflow-editor.js services/admin_web/static/workflow-editor.css
  git commit -m "feat: render workflow canvas and support node dragging"
  ```

- [ ] **Step 6: 浏览器手工验证**

  ```bash
  # 服务器重建并部署 admin 镜像
  ssh -o BatchMode=yes root@100.114.77.27 \
    'cd /docker/web-agent-stack && docker build -f Dockerfile.admin -t webagent/admin-web:deploy-20261108b . && docker compose up -d'
  ```

  打开 `http://100.114.77.27:3001/admin/workflows`，进入某个编排的编辑页，确认：
  画布出现、节点卡片渲染正确、拖动节点后坐标变化、保存后重新打开位置保持。

---

## Task 7: 连线、选择与属性检查器

**Files:**
- Modify: `services/admin_web/static/workflow-editor.js`
- Test: 浏览器手工验证

**Interfaces:**
- Consumes: Task 5 的 `connectNodes` / `removeNode` / `setNodeType` / `nextNodeId`；
  Task 6 的 `render` / `select` / `commit`
- Produces: `#workflow-editor-inspector` 里按选中对象渲染表单

- [ ] **Step 1: 节点面板拖入新节点**

  在 `workflow-editor.js` 里，把「Agent node」「Approval node」两个按钮改成
  可点击新增（拖拽新增是加分项，先保证点击可用）：

  ```javascript
    function bindPalette() {
      document
        .querySelectorAll(".workflow-palette-item")
        .forEach((button) => {
          button.addEventListener("click", () => {
            if (!state.canWrite) {
              return;
            }
            const type = button.getAttribute("data-node-type");
            const next = core.cloneModel(state.model);
            const id = core.nextNodeId(next, type);
            next.nodes.push({
              id,
              type,
              label: "",
              agentCode: type === "agent" ? "" : "",
              outputKey: "",
              prompt: "",
              requiredApprovals: 1,
              approvalTimeoutSec: 0,
              approvalTimeoutAction: "reject",
            });
            next.layout.nodes[id] = {
              x: 60 + next.nodes.length * 32,
              y: 60 + next.nodes.length * 24,
            };
            commit(next);
            select("node", id);
          });
        });
    }
  ```

  并在 `init()` 末尾调用 `bindPalette()`。

- [ ] **Step 2: 从输出端口拉边**

  在 `renderNodes` 里给 `port-out` 绑定 pointer 事件，并在画布上画一条跟随
  指针的临时线：

  ```javascript
    function bindPortDrag(canvas) {
      let pending = null;

      canvas.addEventListener("pointerdown", (event) => {
        const port = event.target.closest(".port-out");
        if (!port || !state.canWrite) {
          return;
        }
        const group = port.closest(".workflow-node");
        pending = { fromId: group.getAttribute("data-node-id") };
        canvas.setPointerCapture(event.pointerId);
        event.preventDefault();
        event.stopPropagation();
      });

      canvas.addEventListener("pointerup", (event) => {
        if (!pending) {
          return;
        }
        const group = event.target.closest(".workflow-node");
        const toId = group ? group.getAttribute("data-node-id") : null;
        if (toId) {
          const next = core.cloneModel(state.model);
          const result = core.connectNodes(next, pending.fromId, toId);
          if (result.ok) {
            commit(next);
          }
        }
        pending = null;
      });
    }
  ```

- [ ] **Step 3: 属性检查器**

  新增 `renderInspector()`，按 `state.selection` 渲染表单：

  ```javascript
    function renderInspector() {
      const host = document.getElementById("workflow-editor-inspector");
      if (!host) {
        return;
      }
      host.innerHTML = "";
      if (!state.selection) {
        renderWorkflowInspector(host);
        return;
      }
      if (state.selection.kind === "node") {
        renderNodeInspector(host, findNode(state.selection.id));
        return;
      }
      renderEdgeInspector(host, findEdge(state.selection.id));
    }

    function findNode(nodeId) {
      return state.model.nodes.find((node) => node.id === nodeId) || null;
    }

    function findEdge(key) {
      const index = Number(String(key).replace("edge-", ""));
      return state.model.edges[index] || null;
    }

    function field(host, labelText, control) {
      const wrapper = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = labelText;
      wrapper.appendChild(caption);
      wrapper.appendChild(control);
      host.appendChild(wrapper);
      return wrapper;
    }

    function textInput(value, onChange) {
      const input = document.createElement("input");
      input.type = "text";
      input.value = value === undefined || value === null ? "" : String(value);
      input.addEventListener("change", () => onChange(input.value));
      return input;
    }

    function numberInput(value, onChange) {
      const input = document.createElement("input");
      input.type = "number";
      input.value = String(value);
      input.addEventListener("change", () => onChange(Number(input.value)));
      return input;
    }

    function selectInput(options, value, onChange) {
      const select = document.createElement("select");
      options.forEach((option) => {
        const element = document.createElement("option");
        element.value = option.value;
        element.textContent = option.label;
        if (option.value === value) {
          element.selected = true;
        }
        select.appendChild(element);
      });
      select.addEventListener("change", () => onChange(select.value));
      return select;
    }

    function renderNodeInspector(host, node) {
      if (!node) {
        return;
      }
      const disabled = !state.canWrite;
      const update = (mutate) => {
        const next = core.cloneModel(state.model);
        const target = next.nodes.find((item) => item.id === node.id);
        mutate(target);
        commit(next);
      };
      const idInput = textInput(node.id, (value) => {
        update((target) => {
          target.id = value;
        });
      });
      idInput.disabled = disabled;
      field(host, "ID", idInput);
      const labelInput = textInput(node.label, (value) => {
        update((target) => {
          target.label = value;
        });
      });
      labelInput.disabled = disabled;
      field(host, "显示名", labelInput);
      const typeSelect = selectInput(
        [
          { value: "agent", label: "Agent" },
          { value: "approval", label: "审批" },
        ],
        node.type,
        (value) => {
          const next = core.setNodeType(state.model, node.id, value);
          commit(next);
        },
      );
      typeSelect.disabled = disabled;
      field(host, "类型", typeSelect);
      if (node.type === "approval") {
        const promptInput = document.createElement("textarea");
        promptInput.rows = 4;
        promptInput.value = node.prompt;
        promptInput.addEventListener("change", () => {
          update((target) => {
            target.prompt = promptInput.value;
          });
        });
        promptInput.disabled = disabled;
        field(host, "审批说明", promptInput);
        const quorum = numberInput(node.requiredApprovals, (value) => {
          update((target) => {
            target.requiredApprovals = value;
          });
        });
        quorum.disabled = disabled;
        field(host, "会签人数", quorum);
        const timeout = numberInput(node.approvalTimeoutSec, (value) => {
          update((target) => {
            target.approvalTimeoutSec = value;
          });
        });
        timeout.disabled = disabled;
        field(host, "超时秒数", timeout);
        const action = selectInput(
          [
            { value: "reject", label: "驳回" },
            { value: "approve", label: "通过" },
          ],
          node.approvalTimeoutAction,
          (value) => {
            update((target) => {
              target.approvalTimeoutAction = value;
            });
          },
        );
        action.disabled = disabled;
        field(host, "超时动作", action);
      } else {
        const agentSelect = selectInput(
          [
            { value: "", label: "（未选择）" },
            ...state.agents.map((agent) => ({
              value: agent.code,
              label: `${agent.name} (${agent.code})`,
            })),
          ],
          node.agentCode,
          (value) => {
            update((target) => {
              target.agentCode = value;
            });
          },
        );
        agentSelect.disabled = disabled;
        field(host, "Agent", agentSelect);
      }
      const outputInput = textInput(node.outputKey, (value) => {
        update((target) => {
          target.outputKey = value;
        });
      });
      outputInput.disabled = disabled;
      field(host, "输出键", outputInput);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-danger btn-small";
      remove.textContent = "删除节点";
      remove.disabled = disabled;
      remove.addEventListener("click", () => {
        const next = core.removeNode(state.model, node.id);
        commit(next);
        select(null, null);
      });
      host.appendChild(remove);
    }

    function renderEdgeInspector(host, edge) {
      if (!edge) {
        return;
      }
      const disabled = !state.canWrite;
      const whenInput = document.createElement("textarea");
      whenInput.rows = 3;
      whenInput.value = edge.when;
      whenInput.addEventListener("change", () => {
        const next = core.cloneModel(state.model);
        const index = state.model.edges.indexOf(edge);
        if (index >= 0) {
          next.edges[index].when = whenInput.value;
        }
        commit(next);
      });
      whenInput.disabled = disabled;
      field(host, `条件 ${edge.from} → ${edge.to}`, whenInput);
      const vars = document.createElement("div");
      vars.className = "workflow-editor-vars";
      ["user_input"].concat(
        state.model.nodes.map((node) => node.outputKey || node.id),
      ).forEach((name) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "btn btn-secondary btn-small";
        chip.textContent = name;
        chip.addEventListener("click", () => {
          const token = name === "user_input" ? name : `upstream.${name}`;
          whenInput.value = `${whenInput.value}${token}`;
        });
        vars.appendChild(chip);
      });
      host.appendChild(vars);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-danger btn-small";
      remove.textContent = "删除边";
      remove.disabled = disabled;
      remove.addEventListener("click", () => {
        const next = core.cloneModel(state.model);
        next.edges = next.edges.filter((item) => item !== edge);
        commit(next);
        select(null, null);
      });
      host.appendChild(remove);
    }

    function renderWorkflowInspector(host) {
      const disabled = !state.canWrite;
      const entrySelect = selectInput(
        state.model.nodes.map((node) => ({
          value: node.id,
          label: node.label || node.id,
        })),
        state.model.entry,
        (value) => {
          const next = core.cloneModel(state.model);
          next.entry = value;
          commit(next);
        },
      );
      entrySelect.disabled = disabled;
      field(host, "入口节点", entrySelect);
      const template = document.createElement("textarea");
      template.rows = 6;
      template.value = state.model.inputTemplate;
      template.addEventListener("change", () => {
        const next = core.cloneModel(state.model);
        next.inputTemplate = template.value;
        commit(next);
      });
      template.disabled = disabled;
      field(host, "输入模板", template);
    }
  ```

  在 `render()` 末尾调用 `renderInspector()`。

- [ ] **Step 4: 键盘删除**

  在 `init()` 里绑定：

  ```javascript
    function bindKeyboard() {
      document.addEventListener("keydown", (event) => {
        if (!state.canWrite || !state.selection) {
          return;
        }
        const tag = (event.target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") {
          return;
        }
        if (event.key !== "Delete" && event.key !== "Backspace") {
          return;
        }
        event.preventDefault();
        if (state.selection.kind === "node") {
          commit(core.removeNode(state.model, state.selection.id));
        } else {
          const edge = findEdge(state.selection.id);
          if (edge) {
            const next = core.cloneModel(state.model);
            next.edges = next.edges.filter((item) => item !== edge);
            commit(next);
          }
        }
        select(null, null);
      });
    }
  ```

- [ ] **Step 5: 语法检查并提交**

  ```bash
  node --check services/admin_web/static/workflow-editor.js
  git add services/admin_web/static/workflow-editor.js
  git commit -m "feat: connect workflow nodes and edit properties in inspector"
  ```

- [ ] **Step 6: 浏览器手工验证**

  重新部署 admin 镜像（Task 6 Step 6 的命令），确认：能从面板加节点、
  能拉出边、能在检查器改 `agentCode` 和 `when`、能删除节点与边、
  切到审批节点后 `agentCode` 被清空。

---

## Task 8: 撤销重做、自动布局、保存与实时校验

**Files:**
- Modify: `services/admin_web/static/workflow-editor.js`
- Modify: `services/admin_web/static/workflow-editor.css`
- Test: 浏览器手工验证 + `tests/test_admin_web.py` 往返保存用例（Task 4 已覆盖骨架）

- [ ] **Step 1: 撤销重做按钮与快捷键**

  ```javascript
    function bindHistory() {
      const undo = document.getElementById("workflow-editor-undo");
      const redo = document.getElementById("workflow-editor-redo");
      if (undo) {
        undo.addEventListener("click", () => {
          const previous = state.stack.undo();
          if (previous) {
            state.model = previous;
            render();
          }
        });
      }
      if (redo) {
        redo.addEventListener("click", () => {
          const next = state.stack.redo();
          if (next) {
            state.model = next;
            render();
          }
        });
      }
      document.addEventListener("keydown", (event) => {
        const tag = (event.target.tagName || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select") {
          return;
        }
        if (!(event.ctrlKey || event.metaKey)) {
          return;
        }
        if (event.key.toLowerCase() === "z" && !event.shiftKey) {
          event.preventDefault();
          const previous = state.stack.undo();
          if (previous) {
            state.model = previous;
            render();
          }
        } else if (
          event.key.toLowerCase() === "z" && event.shiftKey ||
          event.key.toLowerCase() === "y"
        ) {
          event.preventDefault();
          const next = state.stack.redo();
          if (next) {
            state.model = next;
            render();
          }
        }
      });
    }
  ```

- [ ] **Step 2: 自动布局**

  ```javascript
    function bindAutoLayout() {
      const button = document.getElementById("workflow-editor-layout");
      if (!button) {
        return;
      }
      button.addEventListener("click", () => {
        if (!state.canWrite) {
          return;
        }
        const next = core.cloneModel(state.model);
        const layout = core.computeLayout(next);
        next.layout.nodes = layout.nodes;
        next.layout.viewport = layout.viewport;
        commit(next);
      });
    }
  ```

- [ ] **Step 3: 实时校验**

  ```javascript
    function bindValidation() {
      let timer = null;
      const schedule = () => {
        if (!state.canWrite) {
          return;
        }
        if (timer) {
          clearTimeout(timer);
        }
        timer = setTimeout(runValidation, 300);
      };
      window.addEventListener("workflow-editor-changed", schedule);
      state.scheduleValidation = schedule;
    }

    async function runValidation() {
      const bar = document.getElementById("workflow-editor-errorbar");
      const list = document.getElementById("workflow-editor-errors");
      if (!bar || !list) {
        return;
      }
      let body;
      try {
        body = JSON.parse(
          document.getElementById("workflow-editor-definition").value || "{}",
        );
      } catch (err) {
        return;
      }
      const csrfHolder = document.querySelector(
        'input[name="csrf_token"]',
      );
      try {
        const response = await fetch("/admin/workflows/validate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({
            csrf_token: csrfHolder ? csrfHolder.value : "",
            definition: body,
          }),
        });
        if (!response.ok) {
          list.innerHTML = "";
          const item = document.createElement("li");
          item.textContent = "无法完成校验（服务端拒绝）";
          list.appendChild(item);
          bar.hidden = false;
          return;
        }
        const payload = await response.json();
        state.issues = payload.ok ? [] : payload.errors || [];
        list.innerHTML = "";
        state.issues.forEach((issue) => {
          const item = document.createElement("li");
          item.textContent = issue.message;
          list.appendChild(item);
        });
        bar.hidden = state.issues.length === 0;
        render();
      } catch (err) {
        // 校验失败不阻塞编辑：本地编辑仍然可用，只是没有即时反馈。
        state.issues = [];
        list.innerHTML = "";
        bar.hidden = true;
      }
    }
  ```

  在 `commit()` 末尾触发校验：把 `commit` 改成

  ```javascript
    function commit(nextModel) {
      state.stack.push(nextModel);
      state.model = nextModel;
      state.dirty = true;
      render();
      if (state.scheduleValidation) {
        state.scheduleValidation();
      }
    }
  ```

  > **注意**：`syncDefinitionInput()` 必须在 `scheduleValidation()` **之前**执行，
  > 否则校验端点拿到的是上一版 definition。`render()` 内部会调用
  > `syncDefinitionInput()`，所以顺序是对的。

- [ ] **Step 4: JSON 预览切换**

  ```javascript
    function bindJsonToggle() {
      const toggle = document.getElementById("workflow-editor-json-toggle");
      const panel = document.getElementById("workflow-editor-json-panel");
      const view = document.getElementById("workflow-editor-json");
      if (!toggle || !panel || !view) {
        return;
      }
      toggle.addEventListener("click", () => {
        const showing = !panel.hidden;
        panel.hidden = showing;
        if (!showing) {
          view.textContent = JSON.stringify(
            core.toDefinition(state.model),
            null,
            2,
          );
        }
      });
    }
  ```

  > 若 Ludic 的 `hidden=True` 不渲染 `hidden` 属性，改为在 CSS 里用
  > `.workflow-editor-json-panel[hidden] { display: none; }` 兜底，并在 JS 里
  > 用 `panel.style.display` 切换。

- [ ] **Step 5: 把所有绑定接起来**

  在 `init()` 末尾补齐调用：

  ```javascript
      const canvas = document.getElementById("workflow-editor-canvas");
      if (canvas) {
        bindCanvasInteractions(canvas);
        bindPortDrag(canvas);
      }
      bindPalette();
      bindKeyboard();
      bindHistory();
      bindAutoLayout();
      bindValidation();
      bindJsonToggle();
      renderInspector();
  ```

- [ ] **Step 6: 语法检查并提交**

  ```bash
  node --check services/admin_web/static/workflow-editor.js
  git add services/admin_web/static/workflow-editor.js services/admin_web/static/workflow-editor.css
  git commit -m "feat: add workflow editor undo, auto layout and live validation"
  ```

- [ ] **Step 7: 浏览器手工验证**

  重新部署 admin 镜像，确认：
  - 拖节点 → `Ctrl+Z` 回到原位 → `Ctrl+Shift+Z` 恢复；
  - 「Auto layout」后节点按层级排列，重复点击结果一致；
  - 制造一个非法图（例如给一条边填 `upstream.不存在`），底部出现错误条，
    相关边变红；
  - 点「JSON」看到与保存内容一致的 definition；
  - 点「Save workflow」后回到编排页，结构与坐标保持；重新打开仍一致。

---

## Task 9: 端到端回归与文档更新

**Files:**
- Modify: `docs/design/TODO.md`
- Modify: `services/admin_web/README.md`
- Test: 全量回归

**Interfaces:**
- Consumes: Task 1–8 的全部产出
- Produces: 文档与测试一致

- [ ] **Step 1: 跑全量回归**

  ```bash
  # 服务器
  cd /docker/web-agent-stack && docker run --rm --network host \
    -v /docker/web-agent-stack:/src -w /src webagent/tests:oauth-fix \
    python -m pytest tests/test_admin_web.py tests/test_workflow_def.py \
      tests/test_api_e2e.py tests/test_run_graph.py tests/test_migrations.py -q \
      -p no:langsmith --timeout=300

  # 本地
  node --test tests/js/workflow-editor-core.test.mjs
  ```

  Expected: 全部 PASS。

  > `tests/test_api_e2e.py` 覆盖客户端 `/api/workflows`，是「客户端契约不变」的
  > 回归保证。

- [ ] **Step 2: 更新 `services/admin_web/README.md`**

  在「当前页面」清单里把

  ```
  - `/admin/workflows`：Workflow 图定义、启停和删除。
  ```

  改成

  ```
  - `/admin/workflows`：Workflow 图定义、启停和删除。
  - `/admin/workflows/{code}`：Workflow 画布编排器（拖拽节点、连线、条件边、
    属性检查器、实时校验、撤销重做、自动布局）。
  ```

  并在「安全约束」小节补一条：

  ```
  - 画布编排器的校验走 `POST /admin/workflows/validate`，与保存共用同一份
    `app/core/workflow_def.py` 规则；前端不自行判断图是否合法。
  ```

- [ ] **Step 3: 更新 `docs/design/TODO.md`**

  在「用户身份与会话」之前或合适位置新增一条已完成的记录（用 `[x]`）：

  ```markdown
  - [x] **P1：Workflow 编排可视化**
    手写 definition JSON 的后台编辑器已替换为拖拽画布编排器
    （`/admin/workflows/{code}`），见 [V4.8](../../app/design/v4.8-workflow-visual-editor.md)。
    图结构校验规则仍只有一份（`app/core/workflow_def.py`），后台通过
    `POST /admin/workflows/validate` 拿结构化问题，客户端 API 契约不变。
  ```

- [ ] **Step 4: 提交**

  ```bash
  git add docs/design/TODO.md services/admin_web/README.md
  git commit -m "docs: record workflow visual editor rollout"
  ```

- [ ] **Step 5: 最终交付核对**

  ```bash
  git log --oneline -10
  git status --short
  node --test tests/js/workflow-editor-core.test.mjs
  ```

  Expected: 工作区干净；JS 测试全绿；提交历史里能看到 Task 1–9 的分段提交。

---

## 执行顺序与依赖

```
Task 1 (label)  ──┐
                   ├──> Task 2 (collect_graph_issues) ──> Task 3 (validate 端点) ──┐
Task 5 (core.js) ─────────────────────────────────────────────────────────────────┤
                                                                                 ├──> Task 6 (画布) ──> Task 7 (连线/检查器) ──> Task 8 (撤销/布局/校验) ──> Task 9 (回归与文档)
Task 4 (编排页与列表页) ─────────────────────────────────────────────────────────┘
```

- Task 1、Task 4、Task 5 之间无依赖，可并行。
- Task 2 → Task 3 是硬依赖（端点依赖结构化 issue）。
- Task 3 → Task 8 是硬依赖（前端校验依赖端点）。
- Task 4 → Task 6 是硬依赖（页面结构先定，DOM 才有着落）。

