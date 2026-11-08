/**
 * Workflow 画布的 DOM/SVG 交互层。
 *
 * 只做三件事：把 model 画成 SVG、把用户操作翻译成 model 变更、
 * 把 model 变更同步回隐藏表单字段。所有业务判断都委托给
 * globalThis.WorkflowEditorCore。
 *
 * 连线的合法性（自环、重复边、端点不存在）由 core.connectNodes 判定，
 * 本文件只做 reason → 文案的映射，不写第二份实现。
 *
 * 图规则校验（环检测、孤立节点、条件边混用、表达式语法）不在本文件实现，
 * 那是服务端 app/core/workflow_def.py 的唯一实现。
 */
(function () {
  "use strict";

  const core = globalThis.WorkflowEditorCore;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const MIN_SCALE = 0.5;
  const MAX_SCALE = 2;

  const state = {
    model: null,
    stack: null,
    selection: null, // 选中对象：{ kind: "node" | "edge", id: string }
    agents: [],
    canWrite: false,
    code: "",
    viewport: { x: 0, y: 0, scale: 1 },
    issues: [],
    dirty: false,
    // 检查器当前渲染的结构签名；签名没变就不重建检查器（避免焦点丢失）。
    inspectorSignature: null,
    // 检查器重建次数，供测试断言「没有重建」。
    inspectorBuilds: 0,
    // 实时校验的防抖计时器。
    validationTimer: null,
    // 实时校验的防抖间隔（毫秒）。测试可调小以避免等待。
    validationDelayMs: 300,
    // 实时校验总开关。测试里默认关掉，避免防抖计时器跨用例泄漏。
    validationEnabled: true,
  };

  /**
   * 连线被拒绝的原因 → 中文提示。
   *
   * 判定本身**全部**来自 core.connectNodes 返回的 reason，
   * 这里只做文案映射，前端不再写第二份判断。
   */
  const REASON_TEXT = {
    self_loop: "不能连到自己",
    duplicate: "这条边已经存在",
    unknown_node: "目标节点不存在",
  };

  /**
   * 当前正在进行的拖拽：{ kind: "node" | "pan", ... }。
   * 拖拽过程中不写回隐藏字段，结束时统一写一次。
   */
  let drag = null;

  /**
   * 当前正在拉出的连线：{ fromId }。
   * 从节点的输出端口按下时开始，落到目标节点上松手才建边。
   */
  let portDrag = null;

  /** 建一个 SVG 元素。 */
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

  /** 读页面里的初始状态 JSON。 */
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

  /* ------------------------------------------------------------------
     布局坐标的安全读写

     节点 id 是用户可控的（服务端 WorkflowNode.id 无字符集限制），
     因此 id 可能是 "__proto__"。在普通对象上写 obj["__proto__"] = value
     会触发 Object.prototype 的 __proto__ setter：值被写进原型链而不是
     变成自有属性。后果是坐标悄悄丢失，而读取时 obj["__proto__"]
     拿到的又是 Object.prototype，被当成坐标对象后渲染出 NaN。
     所以这里一律用 hasOwnProperty 判存在、用 defineProperty 写。
     ------------------------------------------------------------------ */

  /** 拿到指定 model 的布局坐标表；没有就建一张无原型的表。 */
  function layoutNodesIn(model) {
    if (!model || !model.layout) {
      return null;
    }
    if (!model.layout.nodes) {
      model.layout.nodes = Object.create(null);
    }
    return model.layout.nodes;
  }

  /** 在指定 model 上读某个节点的坐标；没有就返回 null。 */
  function getLayoutPointIn(model, nodeId) {
    const nodes = layoutNodesIn(model);
    if (!nodes || !Object.prototype.hasOwnProperty.call(nodes, nodeId)) {
      return null;
    }
    const point = nodes[nodeId];
    return point && typeof point === "object" ? point : null;
  }

  /** 在指定 model 上写某个节点的坐标。 */
  function setLayoutPointIn(model, nodeId, point) {
    const nodes = layoutNodesIn(model);
    if (!nodes) {
      return;
    }
    // defineProperty 绕开 __proto__ setter，保证任意 id 都得到自有属性。
    Object.defineProperty(nodes, nodeId, {
      value: { x: point.x, y: point.y },
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }

  /** 读当前 model 里的节点坐标（Task 6 的原接口，保持不变）。 */
  function getLayoutPoint(nodeId) {
    return getLayoutPointIn(state.model, nodeId);
  }

  /** 写当前 model 里的节点坐标（Task 6 的原接口，保持不变）。 */
  function setLayoutPoint(nodeId, point) {
    setLayoutPointIn(state.model, nodeId, point);
  }

  /**
   * 给还没有坐标的节点算一份兜底位置，用 core 的分层算法。
   * 算不出来的按序号往后排。
   */
  function computeFallbackPositions() {
    const out = {};
    if (!state.model || !core || typeof core.computeLayout !== "function") {
      return out;
    }
    const missing = state.model.nodes.filter((node) => !getLayoutPoint(node.id));
    if (!missing.length) {
      return out;
    }
    let layout = null;
    try {
      layout = core.computeLayout(state.model);
    } catch (err) {
      layout = null;
    }
    const source = (layout && layout.nodes) || {};
    missing.forEach((node, index) => {
      const point = Object.prototype.hasOwnProperty.call(source, node.id)
        ? source[node.id]
        : null;
      const fallback =
        point && typeof point === "object"
          ? point
          : { x: 40 + index * (core.NODE_WIDTH + 96), y: 40 };
      Object.defineProperty(out, node.id, {
        value: { x: fallback.x, y: fallback.y },
        writable: true,
        enumerable: true,
        configurable: true,
      });
    });
    return out;
  }

  /** 取节点坐标：优先用存好的，没有就用兜底。 */
  function nodePosition(nodeId, fallback) {
    const point = getLayoutPoint(nodeId);
    if (point) {
      return point;
    }
    if (fallback && Object.prototype.hasOwnProperty.call(fallback, nodeId)) {
      return fallback[nodeId];
    }
    return { x: 0, y: 0 };
  }

  /** 把 model 画到画布上。 */
  function render() {
    const canvas = document.getElementById("workflow-editor-canvas");
    if (!canvas || !state.model) {
      return;
    }
    canvas.innerHTML = "";
    canvas.classList.toggle("is-readonly", !state.canWrite);
    canvas.classList.toggle("is-node-dragging", Boolean(drag));
    // 有未保存改动时给画布挂一个标记，CSS 负责显示提示文字。
    canvas.classList.toggle("is-dirty", Boolean(state.dirty));

    const svg = el("svg", { width: "100%", height: "100%" });
    svg.appendChild(buildArrowDefs());
    const view = state.viewport;
    const root = el("g", {
      transform: `translate(${view.x}, ${view.y}) scale(${view.scale})`,
    });
    const edgeLayer = el("g", { class: "workflow-edges" });
    const nodeLayer = el("g", { class: "workflow-nodes" });
    root.appendChild(edgeLayer);
    root.appendChild(nodeLayer);
    svg.appendChild(root);
    canvas.appendChild(svg);

    const fallback = computeFallbackPositions();
    renderEdges(edgeLayer, fallback);
    renderNodes(nodeLayer, fallback);
    // 拖拽过程中不写隐藏字段，拖完再写，避免每次移动都序列化一遍。
    if (!drag) {
      syncDefinitionInput();
    }
  }

  /** 箭头头部的定义。 */
  function buildArrowDefs() {
    const defs = el("defs", {});
    const marker = el("marker", {
      id: "workflow-arrow",
      viewBox: "0 0 10 10",
      refX: "9",
      refY: "5",
      markerWidth: "6",
      markerHeight: "6",
      orient: "auto-start-reverse",
    });
    marker.appendChild(
      el("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "workflow-arrow-head" }),
    );
    defs.appendChild(marker);
    return defs;
  }

  /** 画边。 */
  function renderEdges(layer, fallback) {
    state.model.edges.forEach((edge) => {
      const from = nodePosition(edge.from, fallback);
      const to = nodePosition(edge.to, fallback);
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
        state.selection.id === edgeKey(edge)
      ) {
        group.classList.add("is-selected");
      }
      if (hasEdgeIssue(edge)) {
        group.classList.add("is-error");
      }
      const path = el("path", {
        d: `M ${x1} ${y1} C ${x1 + 60} ${y1}, ${x2 - 60} ${y2}, ${x2} ${y2}`,
        "marker-end": "url(#workflow-arrow)",
      });
      group.appendChild(path);
      if (edge.when) {
        group.appendChild(
          el("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 6 }, edge.when),
        );
      }
      group.addEventListener("click", () => {
        select("edge", edgeKey(edge));
      });
      layer.appendChild(group);
    });
  }

  /**
   * 边的稳定标识：由 from / to 组成，与边在数组里的先后顺序无关。
   *
   * 节点 id 是用户可控的，可能含 "->" 这类字符，所以把 from 的长度
   * 也写进键里：(from, to) → 键是单射，两对不同的端点不会撞成同一个键。
   */
  function edgeKey(edge) {
    const from = String(edge && edge.from !== undefined ? edge.from : "");
    const to = String(edge && edge.to !== undefined ? edge.to : "");
    return `edge:${from.length}:${from}:${to}`;
  }

  /** 由 from / to 拼出边的稳定键。 */
  function edgeKeyOf(from, to) {
    const source = String(from === undefined ? "" : from);
    const target = String(to === undefined ? "" : to);
    return `edge:${source.length}:${source}:${target}`;
  }

  /**
   * 按稳定键反查边。
   *
   * 不做 split("->")：节点 id 里可能有 "->"，拆开再拼回去不可靠。
   * 直接拿每条边的键去比对，从两端匹配，不会认错边。
   */
  function findEdge(key) {
    if (!state.model) {
      return null;
    }
    const target = String(key === undefined ? "" : key);
    return state.model.edges.find((edge) => edgeKey(edge) === target) || null;
  }

  /** 一条 issue 指到的全部节点 id。
   *
   * 服务端既给整组 ``nodes`` 也给首个 ``node``（老字段）；两个都认，缺整组时
   * 退回单值，老客户端不会因为字段升级而丢高亮。
   */
  function issueNodes(issue) {
    if (!issue) {
      return [];
    }
    if (Array.isArray(issue.nodes) && issue.nodes.length) {
      return issue.nodes;
    }
    return issue.node ? [issue.node] : [];
  }

  /** 一条 issue 指到的全部边引用（``from->to``），同上兼容整组与单值。 */
  function issueEdges(issue) {
    if (!issue) {
      return [];
    }
    if (Array.isArray(issue.edges) && issue.edges.length) {
      return issue.edges;
    }
    return issue.edge ? [issue.edge] : [];
  }

  /** 这条边是否被服务端指出了问题。 */
  function hasEdgeIssue(edge) {
    const ref = `${edge.from}->${edge.to}`;
    return state.issues.some((issue) => issueEdges(issue).includes(ref));
  }

  /** 画节点。 */
  function renderNodes(layer, fallback) {
    state.model.nodes.forEach((node) => {
      const position = nodePosition(node.id, fallback);
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
      if (state.issues.some((issue) => issueNodes(issue).includes(node.id))) {
        group.classList.add("is-error");
      }
      if (drag && drag.kind === "node" && drag.nodeId === node.id) {
        group.classList.add("is-dragging");
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
          node.type === "approval" ? "审批" : "Agent",
        ),
      );
      group.appendChild(
        el(
          "text",
          { class: "title", x: 12, y: 44 },
          truncate(node.label || node.outputKey || node.id, 18, 13),
        ),
      );
      group.appendChild(
        el(
          "text",
          { class: "subtitle", x: 12, y: 60 },
          truncate(nodeSubtitle(node), 20, 15),
        ),
      );
      if (state.model.entry === node.id) {
        // 入口节点在左侧挂一个标记（设计文档 :207）。
        group.appendChild(
          el(
            "text",
            {
              class: "entry-mark",
              x: -8,
              y: core.NODE_HEIGHT / 2 + 4,
              "text-anchor": "end",
            },
            "入口",
          ),
        );
      }
      group.appendChild(
        el("circle", {
          class: "port port-in",
          cx: 0,
          cy: core.NODE_HEIGHT / 2,
          r: 6,
        }),
      );
      group.appendChild(
        el("circle", {
          class: "port port-out",
          cx: core.NODE_WIDTH,
          cy: core.NODE_HEIGHT / 2,
          r: 6,
        }),
      );
      layer.appendChild(group);
    });
  }

  /** 节点的副标题：审批节点显示说明首行，agent 节点显示绑定的 agent 名。 */
  function nodeSubtitle(node) {
    if (node.type === "approval") {
      return node.prompt ? node.prompt.split("\n")[0] : "（未填写审批说明）";
    }
    const agent = state.agents.find((item) => item.code === node.agentCode);
    return agent ? agent.name : node.agentCode || "（未选择 Agent）";
  }

  /**
   * 全角字符的码点区间：CJK、假名、谚文、全角标点，以及常见的全角符号与
   * emoji。这些字符在等宽/比例字体里都按一个 em 占位。
   */
  const WIDE_RANGES = [
    [0x1100, 0x115f],
    [0x2e80, 0x303e],
    [0x3041, 0x33ff],
    [0x3400, 0x4dbf],
    [0x4e00, 0x9fff],
    [0xa000, 0xa4cf],
    [0xac00, 0xd7a3],
    [0xf900, 0xfaff],
    [0xfe30, 0xfe6f],
    [0xff00, 0xff60],
    [0xffe0, 0xffe6],
    [0x1f300, 0x1faff],
    [0x20000, 0x3fffd],
  ];

  /** 单个字符占几个 em：全角 1，半角按 0.55 估。 */
  function charEm(char) {
    const code = char.codePointAt(0);
    for (const [low, high] of WIDE_RANGES) {
      if (code >= low && code <= high) {
        return 1;
      }
    }
    return 0.55;
  }

  /** 一段文本大约占几个 em。 */
  function textEm(text) {
    let total = 0;
    for (const char of text) {
      total += charEm(char);
    }
    return total;
  }

  /**
   * 截断过长文本。SVG 的 text 不会自动省略，超长会溢出卡片，
   * 所以这里自己截（设计文档 :205「副标题单行省略」）。
   *
   * 同时看两个上限：``limit`` 是字符数（可读性，别让一行塞太多字），
   * ``maxEm`` 是显示宽度。只看字符数会在中文上溢出近一倍——13px 字号下一个
   * 汉字约 13px 宽，18 个汉字 234px，而卡片正文可用宽度只有约 176px。
   * 半角字符仍然按原来的 18/20 字走，只有全角文本会提前收口。
   */
  function truncate(text, limit, maxEm) {
    const value = String(text === undefined || text === null ? "" : text);
    if (value.length <= limit && textEm(value) <= maxEm) {
      return value;
    }
    const budget = Math.max(1, limit - 1);
    // 省略号按一个全角字符预留，宁可少留半个字也不要把卡片撑破。
    const widthBudget = Math.max(0, maxEm - 1);
    let kept = "";
    let count = 0;
    let used = 0;
    for (const char of value) {
      if (count >= budget) {
        break;
      }
      const em = charEm(char);
      if (used + em > widthBudget) {
        break;
      }
      kept += char;
      count += 1;
      used += em;
    }
    return `${kept}…`;
  }

  /** 把当前 model 写回隐藏的 definition 字段。 */
  function syncDefinitionInput() {
    const input = document.getElementById("workflow-editor-definition");
    if (input && state.model) {
      input.value = JSON.stringify(core.toDefinition(state.model));
    }
  }

  /** 选中一个节点或一条边。选中对象变了 → 检查器必须重建。 */
  function select(kind, id) {
    state.selection = kind ? { kind, id } : null;
    render();
    renderInspector(true);
  }

  /**
   * 提交一份新 model（记一步撤销）。
   *
   * 检查器**不强制重建**：只有结构变了（签名变了）才重建，
   * 单纯的字段值修改不会动检查器 DOM，用户的输入焦点得以保留。
   */
  function commit(nextModel) {
    state.stack.push(nextModel);
    state.model = nextModel;
    state.dirty = true;
    render();
    renderInspector();
    syncHistoryButtons();
    // render() 内部已经同步过隐藏的 definition 字段，
    // 所以这里排队校验时拿到的一定是最新定义。
    scheduleValidation();
  }

  /** 把当前视角写回 model —— 视角要跟着定义一起存。 */
  function writeViewport() {
    if (!state.model || !state.model.layout) {
      return;
    }
    state.model.layout.viewport = {
      x: state.viewport.x,
      y: state.viewport.y,
      scale: state.viewport.scale,
    };
    state.dirty = true;
  }

  /**
   * 绑定画布交互：节点拖拽、画布平移、滚轮缩放、从输出点拉边。
   */
  function bindCanvasInteractions(canvas) {
    let spaceDown = false;

    /** 事件坐标换算成画布坐标（去掉视口平移与缩放）。 */
    function canvasPoint(event) {
      const rect = canvas.getBoundingClientRect();
      return {
        x: (event.clientX - rect.left - state.viewport.x) / state.viewport.scale,
        y: (event.clientY - rect.top - state.viewport.y) / state.viewport.scale,
      };
    }

    canvas.addEventListener("pointerdown", (event) => {
      // 输出端口要先于节点本身判断：端口就在节点卡片里面。
      const port = event.target.closest
        ? event.target.closest(".port-out")
        : null;
      if (port && state.canWrite) {
        const portGroup = port.closest(".workflow-node");
        if (portGroup) {
          portDrag = { fromId: portGroup.getAttribute("data-node-id") };
          event.preventDefault();
          event.stopPropagation();
          return;
        }
      }
      const nodeGroup = event.target.closest
        ? event.target.closest(".workflow-node")
        : null;
      if (nodeGroup) {
        const nodeId = nodeGroup.getAttribute("data-node-id");
        if (!state.canWrite) {
          // 只读用户：点一下只选中，不允许拖动。
          select("node", nodeId);
          return;
        }
        const position = nodePosition(nodeId, computeFallbackPositions());
        const point = canvasPoint(event);
        drag = {
          kind: "node",
          nodeId,
          offsetX: point.x - position.x,
          offsetY: point.y - position.y,
          // 起点坐标：只有真的挪动了才记一步撤销（设计文档 :199）。
          startX: position.x,
          startY: position.y,
          hadStoredPoint: Boolean(getLayoutPoint(nodeId)),
        };
        canvas.setPointerCapture(event.pointerId);
        select("node", nodeId);
        event.preventDefault();
        return;
      }
      // 空白处：中键，或按住空格，才平移。左键空白不做任何事。
      if (event.button !== 1 && !spaceDown) {
        // 左键点空白：取消选中，检查器回到画布态。
        select(null, null);
        return;
      }
      drag = {
        kind: "pan",
        startX: event.clientX,
        startY: event.clientY,
        originX: state.viewport.x,
        originY: state.viewport.y,
      };
      canvas.classList.add("is-panning");
      canvas.setPointerCapture(event.pointerId);
      event.preventDefault();
    });

    canvas.addEventListener("pointermove", (event) => {
      if (!drag) {
        return;
      }
      if (drag.kind === "node") {
        const point = canvasPoint(event);
        // 8px 网格吸附：坐标先落到最近的网格点上。
        setLayoutPoint(drag.nodeId, {
          x:
            Math.round((point.x - drag.offsetX) / core.GRID_SIZE) *
            core.GRID_SIZE,
          y:
            Math.round((point.y - drag.offsetY) / core.GRID_SIZE) *
            core.GRID_SIZE,
        });
        render();
        return;
      }
      state.viewport.x = drag.originX + (event.clientX - drag.startX);
      state.viewport.y = drag.originY + (event.clientY - drag.startY);
      writeViewport();
      render();
    });

    function endDrag(event) {
      if (!drag) {
        return;
      }
      if (drag.kind === "node") {
        if (movedDuringDrag()) {
          // 拖动过程中不记撤销，拖动结束且坐标真的变了才记一步。
          state.stack.push(state.model);
          state.dirty = true;
          syncHistoryButtons();
        } else if (!drag.hadStoredPoint) {
          // 原本没有自有坐标、拖了一圈又回到原地：拖拽过程中写进去的
          // 那个坐标要撤掉，让 model 与拖拽前完全一致（否则会凭空多出
          // 一条 layout 记录，而用户按 Ctrl+Z 又没有可撤销的步骤）。
          const nodes = layoutNodesIn(state.model);
          if (nodes) {
            delete nodes[drag.nodeId];
          }
        }
      }
      drag = null;
      canvas.classList.remove("is-panning");
      // drag 已清空，render 会把最终结果写回隐藏字段。
      render();
      if (
        event &&
        canvas.hasPointerCapture &&
        canvas.hasPointerCapture(event.pointerId)
      ) {
        canvas.releasePointerCapture(event.pointerId);
      }
    }

    /** 这次拖动是不是真的挪动了位置。 */
    function movedDuringDrag() {
      const point = getLayoutPoint(drag.nodeId);
      if (!point) {
        return false;
      }
      return point.x !== drag.startX || point.y !== drag.startY;
    }

    /**
     * 指针被取消（系统手势、触点丢失）：不算拖动完成。
     *
     * 不压撤销栈、不写脏标记，并把坐标还原到起点 ——
     * 取消就是取消，画布不该留下半截改动。
     */
    function cancelPointer() {
      if (portDrag) {
        portDrag = null;
        return;
      }
      if (!drag) {
        return;
      }
      if (drag.kind === "node") {
        if (drag.hadStoredPoint) {
          setLayoutPoint(drag.nodeId, { x: drag.startX, y: drag.startY });
        } else {
          const nodes = layoutNodesIn(state.model);
          if (nodes) {
            delete nodes[drag.nodeId];
          }
        }
      }
      drag = null;
      canvas.classList.remove("is-panning");
      render();
    }

    /**
     * 松手时的收尾：先看是不是在拉边，是的话按落点建边；
     * 否则走节点拖拽 / 画布平移的收尾。
     */
    function endPointer(event) {
      if (portDrag) {
        const fromId = portDrag.fromId;
        portDrag = null;
        const group =
          event && event.target && event.target.closest
            ? event.target.closest(".workflow-node")
            : null;
        const toId = group ? group.getAttribute("data-node-id") : null;
        if (toId) {
          // 拖到空白处松手 → 落点没有节点 → 不建边、不提示。
          connectNodesSafely(fromId, toId);
        }
        return;
      }
      endDrag(event);
    }

    canvas.addEventListener("pointerup", endPointer);
    canvas.addEventListener("pointercancel", cancelPointer);

    // 指针在画布外松手时也要把待连的边作废。
    document.addEventListener("pointerup", () => {
      portDrag = null;
    });

    canvas.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const factor = event.deltaY < 0 ? 1.1 : 0.9;
        const next = Math.min(
          MAX_SCALE,
          Math.max(MIN_SCALE, state.viewport.scale * factor),
        );
        if (next === state.viewport.scale) {
          return;
        }
        // 以指针为中心缩放：指针下的画布坐标在缩放前后保持不变。
        const cx = event.clientX - rect.left;
        const cy = event.clientY - rect.top;
        const px = (cx - state.viewport.x) / state.viewport.scale;
        const py = (cy - state.viewport.y) / state.viewport.scale;
        state.viewport.scale = next;
        state.viewport.x = cx - px * next;
        state.viewport.y = cy - py * next;
        writeViewport();
        render();
      },
      { passive: false },
    );

    // 空格 + 拖拽 = 平移。在输入框里打字时不要把空格当成平移键，
    // 否则在检查器里按空格会把画布拖跑（页面还会跟着空格滚动）。
    document.addEventListener("keydown", (event) => {
      const tag = String(
        (event.target && event.target.tagName) || "",
      ).toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") {
        return;
      }
      if (event.code === "Space" || event.key === " ") {
        spaceDown = true;
      }
    });
    document.addEventListener("keyup", (event) => {
      if (event.code === "Space" || event.key === " ") {
        spaceDown = false;
      }
    });
  }

  /* ------------------------------------------------------------------
     提示条

     页面上有 #workflow-editor-errorbar（默认 hidden）和它里面的
     ul#workflow-editor-errors。Task 8 会复用 showNotice 显示服务端的
     校验结果。
     ------------------------------------------------------------------ */

  /** 在页面上显示一条提示。空消息则清空提示区。 */
  function showNotice(message) {
    const list = document.getElementById("workflow-editor-errors");
    const bar = document.getElementById("workflow-editor-errorbar");
    if (!list) {
      return;
    }
    list.innerHTML = "";
    if (!message) {
      if (bar) {
        bar.setAttribute("hidden", "hidden");
      }
      return;
    }
    const item = document.createElement("li");
    item.textContent = message;
    list.appendChild(item);
    if (bar) {
      bar.removeAttribute("hidden");
    }
  }

  /** reason → 中文提示文案。 */
  function reasonText(reason) {
    if (Object.prototype.hasOwnProperty.call(REASON_TEXT, reason)) {
      return REASON_TEXT[reason];
    }
    return "连边失败";
  }

  /* ------------------------------------------------------------------
     调色板与连线
     ------------------------------------------------------------------ */

  /** 调色板：点一下加一个节点。 */
  function bindPalette() {
    const buttons = document.querySelectorAll
      ? document.querySelectorAll(".workflow-palette-item")
      : [];
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        if (!state.canWrite) {
          return;
        }
        const type =
          button.getAttribute("data-node-type") === "approval"
            ? "approval"
            : "agent";
        const next = core.cloneModel(state.model);
        // nextNodeId 是纯函数：它不改 model，返回一个不与现有 id 冲突的新 id。
        const id = core.nextNodeId(next, type);
        next.nodes.push({
          id,
          type,
          label: "",
          agentCode: "",
          outputKey: "",
          prompt: "",
          requiredApprovals: 1,
          approvalTimeoutSec: 0,
          approvalTimeoutAction: "reject",
        });
        // 必须用安全的写入（defineProperty），不能用普通赋值：
        // 节点 id 用户可控，可能是 __proto__。
        setLayoutPointIn(next, id, { x: 60 + next.nodes.length * 32, y: 60 });
        commit(next);
        select("node", id);
      });
    });
  }

  /**
   * 连一条边。
   *
   * 顺序必须是「先 clone → 再 connect → 再 commit」：
   * connectNodes 是原地修改的，先改再提交的话撤销栈里存的就是连边之后的状态。
   * 合法性判定（自环、重复边、端点不存在）全部来自 connectNodes 的 reason。
   */
  function connectNodesSafely(fromId, toId) {
    if (!state.canWrite) {
      return false;
    }
    const next = core.cloneModel(state.model);
    const result = core.connectNodes(next, fromId, toId);
    if (result.ok) {
      commit(next);
      return true;
    }
    showNotice(reasonText(result.reason));
    return false;
  }

  /* ------------------------------------------------------------------
     属性检查器

     检查器与画布**分开渲染**：render() 只画画布，检查器由 renderInspector()
     负责。字段值改动不重建检查器（否则用户正在编辑的输入框会被销毁，
     焦点丢失、点击落空）；只有「结构变了」才重建。
     ------------------------------------------------------------------ */

  /** 按 id 找节点。 */
  function findNode(nodeId) {
    if (!state.model) {
      return null;
    }
    return state.model.nodes.find((node) => node.id === nodeId) || null;
  }

  /**
   * 检查器结构的签名：选中对象 + 节点集合 + （用到变量时的）可用变量集合。
   * 这些变了表单结构才变；单纯的字段值变化不改变签名。
   */
  function inspectorSignature() {
    if (!state.model) {
      return "none||";
    }
    const selection = state.selection
      ? `${state.selection.kind}:${state.selection.id}`
      : "none";
    const nodes = state.model.nodes
      .map((node) => `${node.id}:${node.type}`)
      .join(",");
    // 变量按钮列表只出现在边检查器与画布检查器里，
    // 所以只有这两种选中态才把变量集合算进签名。
    // 节点检查器里改 outputKey 不重建，用户的输入焦点才留得住。
    const needsVars = !state.selection || state.selection.kind === "edge";
    const vars = needsVars ? availableVariables().join(",") : "";
    return `${selection}|${nodes}|${vars}`;
  }

  /** 检查器只在「结构变了」时重建，字段值改动不重建（避免焦点丢失）。 */
  function renderInspector(force) {
    const host = document.getElementById("workflow-editor-inspector");
    if (!host) {
      return;
    }
    const signature = inspectorSignature();
    if (!force && signature === state.inspectorSignature) {
      return;
    }
    state.inspectorSignature = signature;
    state.inspectorBuilds += 1;
    host.innerHTML = "";
    if (!state.selection) {
      renderWorkflowInspector(host);
      return;
    }
    if (state.selection.kind === "node") {
      const node = findNode(state.selection.id);
      if (node) {
        renderNodeInspector(host, node);
        return;
      }
      renderWorkflowInspector(host);
      return;
    }
    const edge = findEdge(state.selection.id);
    if (edge) {
      renderEdgeInspector(host, edge);
      return;
    }
    renderWorkflowInspector(host);
  }

  /** 一个带标题的表单项。控件上带 data-field，方便定位。 */
  function field(name, labelText, control) {
    control.setAttribute("data-field", name);
    const label = document.createElement("label");
    label.textContent = labelText;
    label.appendChild(control);
    return label;
  }

  /** 单行文本框。 */
  function textInput(value, onChange) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value === undefined || value === null ? "" : String(value);
    input.disabled = !state.canWrite;
    input.addEventListener("change", () => onChange(input.value));
    return input;
  }

  /** 数字输入框。 */
  function numberInput(value, onChange) {
    const input = document.createElement("input");
    input.type = "number";
    input.value = String(value === undefined || value === null ? 0 : value);
    input.disabled = !state.canWrite;
    input.addEventListener("change", () => {
      const parsed = Number(input.value);
      onChange(Number.isFinite(parsed) ? parsed : 0);
    });
    return input;
  }

  /** 下拉框。options 是 [{ value, label }]。 */
  function selectInput(value, options, onChange) {
    const select = document.createElement("select");
    options.forEach((option) => {
      const item = document.createElement("option");
      item.value = String(option.value);
      item.textContent =
        option.label === undefined ? String(option.value) : option.label;
      item.selected = String(option.value) === String(value);
      select.appendChild(item);
    });
    select.value = String(value === undefined || value === null ? "" : value);
    select.disabled = !state.canWrite;
    select.addEventListener("change", () => onChange(select.value));
    return select;
  }

  /** 文本域（审批说明、when、输入模板用）。 */
  function textArea(value, rows, onChange) {
    const input = document.createElement("textarea");
    input.rows = rows;
    input.value = value === undefined || value === null ? "" : String(value);
    input.disabled = !state.canWrite;
    input.addEventListener("change", () => onChange(input.value));
    return input;
  }

  /** 一个普通按钮。 */
  function actionButton(text, className, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = text;
    button.disabled = !state.canWrite;
    button.addEventListener("click", onClick);
    return button;
  }

  /**
   * 可用的 when 变量。
   *
   * 只认两种写法：user_input 与 upstream.<outputKey>。
   * 标识符必须是 [A-Za-z_][A-Za-z0-9_]*（见 app/core/expr.py 的词法），
   * 所以不合法的 outputKey 不提供按钮 —— 插进去必定是语法错误。
   * 节点没填 outputKey 时退回节点 id（与 WorkflowNode.key 的取法一致）。
   */
  function availableVariables() {
    const names = [];
    if (!state.model) {
      return ["user_input"];
    }
    state.model.nodes.forEach((node) => {
      const key = node.outputKey || node.id;
      if (
        /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) &&
        !names.includes(key)
      ) {
        names.push(key);
      }
    });
    return ["user_input"].concat(names);
  }

  /** 变量按钮插入的 token：user_input 或 upstream.<outputKey>。 */
  function variableToken(name) {
    return name === "user_input" ? "user_input" : `upstream.${name}`;
  }

  /**
   * 把 token 插进文本域的光标处。
   *
   * 与相邻标识符直接相连会被粘成另一个标识符，所以至少要补空格。
   * 但只补空格还不够：左边已经是一个**完整操作数**时，裸 token 接上去
   * 是非法表达式（`user_input upstream.plan` 会被 expr.py 拒成
   * 「表达式结尾有多余内容」）。所以这种情况要补一个 ` and `。
   *
   * 规则：插入点左侧去掉尾部空白后，最后一个字符若属于操作数结尾
   * （标识符字符 / 右括号 / 引号），就说明左边表达式已完整 → 用 ` and `；
   * 否则（左边是运算符、空白或空串）插裸 token。
   */
  function insertAtCursor(textarea, token) {
    const value = textarea.value || "";
    const start = Number.isInteger(textarea.selectionStart)
      ? textarea.selectionStart
      : value.length;
    const end = Number.isInteger(textarea.selectionEnd)
      ? textarea.selectionEnd
      : start;
    const before = value.slice(0, start);
    const after = value.slice(end);
    const trimmed = before.replace(/\s+$/, "");
    const leftIsOperandEnd = /[A-Za-z0-9_)\]"']$/.test(trimmed);
    const prefix = leftIsOperandEnd ? " and " : before ? " " : "";
    const right = after && /^[A-Za-z0-9_]/.test(after) ? " " : "";
    textarea.value = `${before}${prefix}${token}${right}${after}`;
    const caret =
      before.length + prefix.length + token.length + right.length;
    textarea.selectionStart = caret;
    textarea.selectionEnd = caret;
  }

  /** 改节点的一个字段（不含 id）。 */
  function updateNodeField(nodeId, name, value) {
    if (!state.canWrite) {
      return;
    }
    const next = core.cloneModel(state.model);
    const node = next.nodes.find((item) => item.id === nodeId);
    if (!node) {
      return;
    }
    node[name] = value;
    commit(next);
  }

  /** 改一条边的 when。 */
  function updateEdgeWhen(from, to, value) {
    if (!state.canWrite) {
      return;
    }
    const next = core.cloneModel(state.model);
    const edge = next.edges.find(
      (item) => item.from === from && item.to === to,
    );
    if (!edge) {
      return;
    }
    edge.when = value;
    commit(next);
  }

  /** 把布局坐标的键从旧 id 迁到新 id。 */
  function moveLayoutPoint(model, oldId, newId) {
    const nodes = model && model.layout ? model.layout.nodes : null;
    if (!nodes || !Object.prototype.hasOwnProperty.call(nodes, oldId)) {
      return;
    }
    const point = nodes[oldId];
    const safe = point && typeof point === "object" ? point : { x: 0, y: 0 };
    Object.defineProperty(nodes, newId, {
      value: { x: safe.x, y: safe.y },
      writable: true,
      enumerable: true,
      configurable: true,
    });
    delete nodes[oldId];
  }

  /**
   * 改节点 id —— 完整改名。
   *
   * 节点 id 是引用键：edges[].from/to、definition.entry、layout.nodes 的键
   * 全都指着它，只改 node.id 会立刻造出一堆坏引用。
   */
  function renameNode(oldId, rawValue) {
    if (!state.canWrite) {
      return false;
    }
    const newId = String(rawValue === undefined ? "" : rawValue).trim();
    if (!newId) {
      showNotice("节点 id 不能为空");
      return false;
    }
    if (newId === oldId) {
      return true;
    }
    const next = core.cloneModel(state.model);
    if (next.nodes.some((node) => node.id === newId)) {
      showNotice(`节点 id ${newId} 已经存在`);
      return false;
    }
    const node = next.nodes.find((item) => item.id === oldId);
    if (!node) {
      return false;
    }
    node.id = newId;
    next.edges.forEach((edge) => {
      if (edge.from === oldId) {
        edge.from = newId;
      }
      if (edge.to === oldId) {
        edge.to = newId;
      }
    });
    if (next.entry === oldId) {
      next.entry = newId;
    }
    moveLayoutPoint(next, oldId, newId);
    // 选中态跟着改，否则检查器会以为选中的节点不见了。
    if (
      state.selection &&
      state.selection.kind === "node" &&
      state.selection.id === oldId
    ) {
      state.selection.id = newId;
    }
    // 改名会改掉边的 from/to，边的稳定键也跟着变 —— 选中的边要换新键。
    if (
      state.selection &&
      state.selection.kind === "edge" &&
      state.selection.id
    ) {
      const selected = findEdge(state.selection.id);
      if (selected && (selected.from === oldId || selected.to === oldId)) {
        state.selection.id = edgeKeyOf(
          selected.from === oldId ? newId : selected.from,
          selected.to === oldId ? newId : selected.to,
        );
      }
    }
    commit(next);
    return true;
  }

  /** 节点检查器。 */
  function renderNodeInspector(host, node) {
    const nodeId = node.id;
    host.appendChild(
      field(
        "type",
        "节点类型",
        selectInput(
          node.type,
          [
            { value: "agent", label: "Agent" },
            { value: "approval", label: "审批" },
          ],
          (value) => {
            if (!state.canWrite) {
              return;
            }
            // setNodeType 内部 clone 并清掉另一类型的专属字段。
            commit(core.setNodeType(state.model, nodeId, value));
          },
        ),
      ),
    );
    const idInput = textInput(nodeId, (value) => {
      if (!renameNode(nodeId, value)) {
        // 改名被拒：把输入框里的非法值改回去。
        idInput.value = nodeId;
      }
    });
    host.appendChild(field("id", "节点 ID", idInput));
    host.appendChild(
      field(
        "label",
        "标题",
        textInput(node.label, (value) =>
          updateNodeField(nodeId, "label", value),
        ),
      ),
    );
    if (node.type === "approval") {
      host.appendChild(
        field(
          "prompt",
          "审批说明",
          textArea(node.prompt, 4, (value) =>
            updateNodeField(nodeId, "prompt", value),
          ),
        ),
      );
      host.appendChild(
        field(
          "requiredApprovals",
          "会签人数",
          numberInput(node.requiredApprovals, (value) =>
            updateNodeField(nodeId, "requiredApprovals", value),
          ),
        ),
      );
      host.appendChild(
        field(
          "approvalTimeoutSec",
          "超时秒数（0 表示不超时）",
          numberInput(node.approvalTimeoutSec, (value) =>
            updateNodeField(nodeId, "approvalTimeoutSec", value),
          ),
        ),
      );
      host.appendChild(
        field(
          "approvalTimeoutAction",
          "超时动作",
          selectInput(
            node.approvalTimeoutAction,
            [
              { value: "reject", label: "驳回" },
              { value: "approve", label: "通过" },
            ],
            (value) =>
              updateNodeField(nodeId, "approvalTimeoutAction", value),
          ),
        ),
      );
    } else {
      const agentOptions = [{ value: "", label: "（未选择）" }].concat(
        state.agents.map((agent) => ({
          value: agent.code,
          label: `${agent.name}（${agent.code}）`,
        })),
      );
      host.appendChild(
        field(
          "agentCode",
          "Agent",
          selectInput(node.agentCode, agentOptions, (value) =>
            updateNodeField(nodeId, "agentCode", value),
          ),
        ),
      );
      host.appendChild(
        field(
          "outputKey",
          "输出键",
          textInput(node.outputKey, (value) =>
            updateNodeField(nodeId, "outputKey", value),
          ),
        ),
      );
    }
    host.appendChild(
      actionButton("删除节点", "btn btn-danger btn-small", () => {
        if (!state.canWrite) {
          return;
        }
        commit(core.removeNode(state.model, nodeId));
        select(null, null);
      }),
    );
  }

  /** 边检查器。 */
  function renderEdgeInspector(host, edge) {
    const from = edge.from;
    const to = edge.to;
    const whenInput = textArea(edge.when, 3, (value) =>
      updateEdgeWhen(from, to, value),
    );
    host.appendChild(field("when", "条件（when）", whenInput));
    const variables = document.createElement("div");
    variables.className = "workflow-inspector-variables";
    availableVariables().forEach((name) => {
      const token = variableToken(name);
      const button = actionButton(
        token,
        "btn btn-secondary btn-small workflow-variable-button",
        () => insertAtCursor(whenInput, token),
      );
      button.setAttribute("data-variable", token);
      // 按下按钮时不让文本域失焦：光标位置要留住。
      button.addEventListener("mousedown", (event) => {
        if (event && event.preventDefault) {
          event.preventDefault();
        }
      });
      variables.appendChild(button);
    });
    host.appendChild(variables);
    host.appendChild(
      actionButton("删除边", "btn btn-danger btn-small", () => {
        if (!state.canWrite) {
          return;
        }
        const next = core.cloneModel(state.model);
        next.edges = next.edges.filter(
          (item) => !(item.from === from && item.to === to),
        );
        commit(next);
        select(null, null);
      }),
    );
  }

  /** 画布检查器：未选中任何对象时。 */
  function renderWorkflowInspector(host) {
    const entryOptions = [{ value: "", label: "（未设置）" }].concat(
      state.model.nodes.map((node) => ({
        value: node.id,
        label: node.label ? `${node.label}（${node.id}）` : node.id,
      })),
    );
    host.appendChild(
      field(
        "entry",
        "入口节点",
        selectInput(state.model.entry, entryOptions, (value) => {
          if (!state.canWrite) {
            return;
          }
          const next = core.cloneModel(state.model);
          next.entry = value;
          commit(next);
        }),
      ),
    );
    host.appendChild(
      field(
        "inputTemplate",
        "输入模板",
        textArea(state.model.inputTemplate, 6, (value) => {
          if (!state.canWrite) {
            return;
          }
          const next = core.cloneModel(state.model);
          next.inputTemplate = value;
          commit(next);
        }),
      ),
    );
    const hint = document.createElement("div");
    hint.className = "workflow-inspector-hint";
    // upstream 是服务端认可的保留变量（WorkflowDefinition._template_vars
    // 的 known 集里就有它），默认 inputTemplate 也在用它，所以必须列出来。
    hint.textContent =
      "可用变量：" +
      ["user_input", "upstream"].concat(availableVariables().slice(1)).join("、") +
      "（模板里写 {{变量名}}，不写 upstream. 前缀）";
    host.appendChild(hint);
  }

  /* ------------------------------------------------------------------
     撤销 / 重做
     ------------------------------------------------------------------ */

  /** 撤销 / 重做：工具栏按钮与快捷键。 */
  function bindHistory() {
    const undo = document.getElementById("workflow-editor-undo");
    const redo = document.getElementById("workflow-editor-redo");
    if (undo) {
      undo.addEventListener("click", () => {
        applyHistory(state.stack.undo());
      });
    }
    if (redo) {
      redo.addEventListener("click", () => {
        applyHistory(state.stack.redo());
      });
    }
    document.addEventListener("keydown", (event) => {
      const target = event.target || {};
      const tag = String(target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") {
        // 输入框里按 Ctrl+Z 交给浏览器的输入撤销，画布不抢。
        return;
      }
      if (!(event.ctrlKey || event.metaKey)) {
        return;
      }
      const key = String(event.key || "").toLowerCase();
      if (key === "z" && !event.shiftKey) {
        event.preventDefault();
        applyHistory(state.stack.undo());
        return;
      }
      if ((key === "z" && event.shiftKey) || key === "y") {
        event.preventDefault();
        applyHistory(state.stack.redo());
      }
    });
    syncHistoryButtons();
  }

  /** 撤销/重做按钮的可用状态。 */
  function syncHistoryButtons() {
    const undo = document.getElementById("workflow-editor-undo");
    const redo = document.getElementById("workflow-editor-redo");
    if (undo) {
      undo.disabled = !state.stack.canUndo();
    }
    if (redo) {
      redo.disabled = !state.stack.canRedo();
    }
  }

  /**
   * 把栈里取出的快照装回当前状态。
   *
   * 撤销之后选中的对象可能已经不存在（或被换成了别的东西），
   * 所以先按**稳定标识**重新定位：节点按 id、边按 from/to。
   */
  function applyHistory(snapshot) {
    if (!snapshot) {
      return;
    }
    state.model = snapshot;
    state.dirty = true;
    state.selection = relocateSelection();
    render();
    renderInspector();
    syncHistoryButtons();
    scheduleValidation();
  }

  /** 撤销/重做之后重新定位选中态；定位不到就清空。 */
  function relocateSelection() {
    if (!state.selection) {
      return null;
    }
    if (state.selection.kind === "node") {
      const node = findNode(state.selection.id);
      return node ? { kind: "node", id: node.id } : null;
    }
    const edge = findEdge(state.selection.id);
    return edge ? { kind: "edge", id: edgeKey(edge) } : null;
  }

  /* ------------------------------------------------------------------
     整理布局 / 适应画布
     ------------------------------------------------------------------ */

  /** 整理布局：按最长路径分层重排坐标。 */
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
      // 只重排坐标。computeLayout 返回的 viewport 是 {0,0,1}，
      // 直接覆盖会把用户保存的视角重置掉，所以保留原视角。
      next.layout.nodes = layout.nodes;
      commit(next);
      select(null, null);
    });
  }

  /** 适应画布：只改视口，不改节点坐标，也不记撤销。 */
  function bindFitCanvas() {
    const button = document.getElementById("workflow-editor-fit");
    if (!button) {
      return;
    }
    button.addEventListener("click", () => {
      fitCanvas();
    });
  }

  /** 把所有节点框进画布可视范围。 */
  function fitCanvas() {
    const canvas = document.getElementById("workflow-editor-canvas");
    if (!canvas || !state.model || !state.model.nodes.length) {
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const points = state.model.nodes.map((node) => nodePosition(node.id));
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    points.forEach((point) => {
      minX = Math.min(minX, point.x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, point.x + core.NODE_WIDTH);
      maxY = Math.max(maxY, point.y + core.NODE_HEIGHT);
    });
    const padding = 40;
    const width = Math.max(1, maxX - minX);
    const height = Math.max(1, maxY - minY);
    const viewportWidth = Number(rect.width) > 0 ? Number(rect.width) : 800;
    const viewportHeight = Number(rect.height) > 0 ? Number(rect.height) : 600;
    const fitScale = Math.min(
      (viewportWidth - padding * 2) / width,
      (viewportHeight - padding * 2) / height,
    );
    const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, fitScale));
    state.viewport.scale = scale;
    state.viewport.x = padding - minX * scale;
    state.viewport.y = padding - minY * scale;
    writeViewport();
    render();
  }

  /* ------------------------------------------------------------------
     实时校验
     ------------------------------------------------------------------ */

  /** 实时校验：改动后防抖调用服务端校验端点。 */
  function bindValidation() {
    if (!state.canWrite) {
      return;
    }
    scheduleValidation();
  }

  /** 防抖后跑一次校验。 */
  function scheduleValidation() {
    if (!state.validationEnabled || !state.canWrite) {
      return;
    }
    if (typeof globalThis.fetch !== "function") {
      // 没有 fetch 就没有实时反馈，静默跳过，不阻塞编辑。
      return;
    }
    if (state.validationTimer) {
      clearTimeout(state.validationTimer);
    }
    state.validationTimer = setTimeout(() => {
      state.validationTimer = null;
      runValidation();
    }, state.validationDelayMs);
  }

  /** 关掉校验：清掉待触发的防抖计时器。 */
  function cancelValidation() {
    if (state.validationTimer) {
      clearTimeout(state.validationTimer);
      state.validationTimer = null;
    }
  }

  /** 端点的机器码 → 中文文案。 */
  function machineText(code) {
    const table = {
      invalid_json: "请求格式不正确",
      definition_required: "没有提交工作流定义",
      definition_shape_invalid: "工作流定义结构不正确",
      forbidden: "没有校验权限或登录已过期",
    };
    return table[code] || "无法完成校验";
  }

  /** 调校验端点，把结果显示到错误条。 */
  async function runValidation() {
    const bar = document.getElementById("workflow-editor-errorbar");
    const list = document.getElementById("workflow-editor-errors");
    const definitionInput = document.getElementById("workflow-editor-definition");
    if (!bar || !list || !definitionInput) {
      return;
    }
    let definition;
    try {
      definition = JSON.parse(definitionInput.value || "{}");
    } catch (err) {
      return;
    }
    const csrfInput = document.querySelector('input[name="csrf_token"]');
    try {
      const response = await globalThis.fetch("/admin/workflows/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          csrf_token: csrfInput ? csrfInput.value : "",
          definition,
        }),
      });
      let payload = null;
      try {
        payload = await response.json();
      } catch (err) {
        payload = null;
      }
      if (!response.ok) {
        // 400 / 403：body 里是机器码，翻成中文再显示。
        const first =
          payload && payload.errors && payload.errors[0]
            ? payload.errors[0].message
            : "";
        state.issues = [];
        showIssues([machineText(first)]);
        render();
        return;
      }
      state.issues =
        payload && payload.ok === false ? payload.errors || [] : [];
      showIssues(state.issues.map((issue) => issue && issue.message));
      render();
    } catch (err) {
      // 校验失败**不阻塞编辑**：本地编辑仍然可用，只是没有即时反馈。
      state.issues = [];
      showIssues([]);
      render();
    }
  }

  /** 把一组文案显示到错误条上；空数组则隐藏错误条。 */
  function showIssues(messages) {
    const bar = document.getElementById("workflow-editor-errorbar");
    const list = document.getElementById("workflow-editor-errors");
    if (!list) {
      return;
    }
    list.innerHTML = "";
    const texts = (messages || []).filter(
      (text) => text !== undefined && text !== null && String(text) !== "",
    );
    texts.forEach((text) => {
      const item = document.createElement("li");
      item.textContent = String(text);
      list.appendChild(item);
    });
    if (bar) {
      if (texts.length) {
        bar.removeAttribute("hidden");
      } else {
        bar.setAttribute("hidden", "hidden");
      }
    }
  }

  /* ------------------------------------------------------------------
     JSON 预览
     ------------------------------------------------------------------ */

  /** 切换 JSON 预览面板。 */
  function bindJsonToggle() {
    const toggle = document.getElementById("workflow-editor-json-toggle");
    const panel = document.getElementById("workflow-editor-json-panel");
    const view = document.getElementById("workflow-editor-json");
    if (!toggle || !panel || !view) {
      return;
    }
    toggle.addEventListener("click", () => {
      const showing = !panel.hidden;
      if (!showing) {
        // 面板里是页面渲染时的初始定义，展开时先刷新成当前内容。
        view.textContent = JSON.stringify(
          core.toDefinition(state.model),
          null,
          2,
        );
      }
      panel.hidden = showing;
    });
  }

  /* ------------------------------------------------------------------
     键盘删除
     ------------------------------------------------------------------ */

  /** Delete / Backspace 删除选中的节点或边。 */
  function bindKeyboard() {
    document.addEventListener("keydown", (event) => {
      if (!state.canWrite || !state.selection) {
        return;
      }
      const target = event.target || {};
      const tag = String(target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") {
        return;
      }
      if (event.key !== "Delete" && event.key !== "Backspace") {
        return;
      }
      event.preventDefault();
      deleteSelection();
    });
  }

  /** 删除当前选中的对象。 */
  function deleteSelection() {
    if (!state.selection) {
      return;
    }
    if (state.selection.kind === "node") {
      const node = findNode(state.selection.id);
      if (!node) {
        select(null, null);
        return;
      }
      commit(core.removeNode(state.model, node.id));
      select(null, null);
      return;
    }
    const edge = findEdge(state.selection.id);
    if (!edge) {
      select(null, null);
      return;
    }
    const next = core.cloneModel(state.model);
    next.edges = next.edges.filter(
      (item) => !(item.from === edge.from && item.to === edge.to),
    );
    commit(next);
    select(null, null);
  }

  /** 初始化。 */
  function init() {
    const payload = readState();
    if (!payload || !core) {
      return;
    }
    try {
      state.model = core.toModel(payload.definition || {});
    } catch (err) {
      // 定义坏到连模型都建不出来时，退回空画布，不要白屏。
      state.model = core.toModel({});
    }
    state.stack = core.createUndoStack(50);
    state.stack.reset(state.model);
    state.agents = payload.agents || [];
    state.canWrite = Boolean(payload.canWrite);
    state.code = payload.code || "";
    const viewport = state.model.layout.viewport || {};
    state.viewport = {
      x: Number(viewport.x || 0),
      y: Number(viewport.y || 0),
      scale: Number(viewport.scale || 1),
    };
    render();
    renderInspector(true);
    const canvas = document.getElementById("workflow-editor-canvas");
    if (canvas) {
      bindCanvasInteractions(canvas);
    }
    bindPalette();
    bindKeyboard();
    bindHistory();
    bindAutoLayout();
    bindFitCanvas();
    bindJsonToggle();
    bindValidation();
  }

  const api = {
    state,
    render,
    select,
    commit,
    getLayoutPoint,
    setLayoutPoint,
    getLayoutPointIn,
    setLayoutPointIn,
    renderInspector,
    showNotice,
    applyHistory,
    fitCanvas,
    scheduleValidation,
    runValidation,
    cancelValidation,
    syncHistoryButtons,
    edgeKey,
  };

  if (typeof window !== "undefined") {
    window.WorkflowEditor = api;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
