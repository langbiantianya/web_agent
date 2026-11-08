/**
 * Workflow 画布的 DOM/SVG 交互层。
 *
 * 只做三件事：把 model 画成 SVG、把用户操作翻译成 model 变更、
 * 把 model 变更同步回隐藏表单字段。所有业务判断都委托给
 * globalThis.WorkflowEditorCore。
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
  };

  /**
   * 当前正在进行的拖拽：{ kind: "node" | "pan", ... }。
   * 拖拽过程中不写回隐藏字段，结束时统一写一次。
   */
  let drag = null;

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

  /** 拿到布局坐标表；没有就建一张无原型的表。 */
  function layoutNodes() {
    if (!state.model || !state.model.layout) {
      return null;
    }
    if (!state.model.layout.nodes) {
      state.model.layout.nodes = Object.create(null);
    }
    return state.model.layout.nodes;
  }

  /** 读某个节点的坐标；没有就返回 null。 */
  function getLayoutPoint(nodeId) {
    const nodes = layoutNodes();
    if (!nodes || !Object.prototype.hasOwnProperty.call(nodes, nodeId)) {
      return null;
    }
    const point = nodes[nodeId];
    return point && typeof point === "object" ? point : null;
  }

  /** 写某个节点的坐标。 */
  function setLayoutPoint(nodeId, point) {
    const nodes = layoutNodes();
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
    state.model.edges.forEach((edge, index) => {
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
        state.selection.id === edgeKey(index)
      ) {
        group.classList.add("is-selected");
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
        select("edge", edgeKey(index));
      });
      layer.appendChild(group);
    });
  }

  /** 边的稳定标识：按序号。 */
  function edgeKey(index) {
    return `edge-${index}`;
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
      if (state.issues.some((issue) => issue.node === node.id)) {
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
          truncate(node.label || node.outputKey || node.id, 18),
        ),
      );
      group.appendChild(
        el(
          "text",
          { class: "subtitle", x: 12, y: 60 },
          truncate(nodeSubtitle(node), 20),
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
   * 截断过长文本。SVG 的 text 不会自动省略，超长会溢出卡片，
   * 所以这里自己截（设计文档 :205「副标题单行省略」）。
   */
  function truncate(text, limit) {
    const value = String(text === undefined || text === null ? "" : text);
    if (value.length <= limit) {
      return value;
    }
    return `${value.slice(0, Math.max(1, limit - 1))}…`;
  }

  /** 把当前 model 写回隐藏的 definition 字段。 */
  function syncDefinitionInput() {
    const input = document.getElementById("workflow-editor-definition");
    if (input && state.model) {
      input.value = JSON.stringify(core.toDefinition(state.model));
    }
  }

  /** 选中一个节点或一条边。 */
  function select(kind, id) {
    state.selection = kind ? { kind, id } : null;
    render();
  }

  /** 提交一份新 model（记一步撤销）。 */
  function commit(nextModel) {
    state.stack.push(nextModel);
    state.model = nextModel;
    state.dirty = true;
    render();
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
   * 绑定画布交互：节点拖拽、画布平移、滚轮缩放。
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
        };
        canvas.setPointerCapture(event.pointerId);
        select("node", nodeId);
        event.preventDefault();
        return;
      }
      // 空白处：中键，或按住空格，才平移。左键空白不做任何事。
      if (event.button !== 1 && !spaceDown) {
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
        // 拖动过程中不记撤销，拖动结束才记一步。
        state.stack.push(state.model);
        state.dirty = true;
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

    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

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

    // 空格 + 拖拽 = 平移。
    document.addEventListener("keydown", (event) => {
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
    const canvas = document.getElementById("workflow-editor-canvas");
    if (canvas) {
      bindCanvasInteractions(canvas);
    }
  }

  const api = {
    state,
    render,
    select,
    commit,
    getLayoutPoint,
    setLayoutPoint,
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
