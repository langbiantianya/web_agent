/**
 * Workflow 画布的纯逻辑层。
 *
 * 本文件是**零 DOM 依赖**的纯脚本：不引用任何浏览器宿主对象，
 * 所有函数必须能脱离浏览器裸跑，
 * 测试文件 tests/js/workflow-editor-core.test.mjs 依赖这一点。
 *
 * 画布不持有第二份状态：definition ⇄ model 的转换是唯一的真相来源。
 *
 * 图规则校验（环、孤立节点、条件边混用、表达式语法）不在本文件实现，
 * 那是服务端 app/core/workflow_def.py 的职责。
 */
(function () {
  "use strict";

  const NODE_WIDTH = 200;
  const NODE_HEIGHT = 72;
  const GRID_SIZE = 8;
  const LAYOUT_GAP_X = 96;
  const LAYOUT_GAP_Y = 48;
  const LAYOUT_MARGIN = 40;

  /** 深拷贝（纯 JSON 结构），用于隔离 model 与撤销栈里的快照。 */
  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  /** 把任意来源的节点补齐成完整结构，缺失字段一律给零值。 */
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

  /** 把任意来源的边补齐成完整结构。 */
  function normalizeEdge(raw) {
    return {
      from: String((raw && raw.from) || ""),
      to: String((raw && raw.to) || ""),
      when: String((raw && raw.when) || ""),
    };
  }

  /** definition → model。 */
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

  /** model → definition，输出里不出现 null 字段。 */
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

  /** 复制整个 model。 */
  function cloneModel(model) {
    return clone(model);
  }

  /** 生成不与现有节点冲突的新节点 id。 */
  function nextNodeId(model, type) {
    const prefix = type === "approval" ? "approval" : "step";
    const used = new Set(model.nodes.map((node) => node.id));
    let index = 1;
    while (used.has(`${prefix}${index}`)) {
      index += 1;
    }
    return `${prefix}${index}`;
  }

  /**
   * 原地新增一条边。只做最基础的连线合法性检查：
   * 自连接、重复边、端点不存在。是否成环由服务端校验负责。
   */
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

  /** 删除节点，同时清掉挂在它上面的边与布局坐标；返回新 model。 */
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

  /** 切换节点类型，同时清掉另一类型的专属字段；返回新 model。 */
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

  /**
   * 按最长路径分层计算节点坐标：同层节点纵向排开，层与层之间横向推进。
   * 同一输入必定得到同一结果。
   */
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
    // 最长路径分层：反复松弛，直到不再变化。成环的图由服务端校验拦截，
    // 这里取最后一次松弛的结果即可。
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

  /**
   * 创建撤销/重做栈。栈里存的是 model 的快照副本。
   */
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
