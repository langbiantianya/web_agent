import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

import { createDom } from "./workflow-editor.dom-stub.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..", "..");
const corePath = path.join(root, "services/admin_web/static/workflow-editor-core.js");
const editorPath = path.join(root, "services/admin_web/static/workflow-editor.js");

const coreSrc = fs.readFileSync(corePath, "utf8");
const editorSrc = fs.readFileSync(editorPath, "utf8");

function loadCore() {
  // 用 new Function 在**当前 realm** 求值，而不是 node:vm 沙箱：
  // vm 里造出来的对象带着另一个 realm 的 Object.prototype，
  // assert/strict 的 deepStrictEqual 会以「结构相同但原型不同」失败。
  return new Function(`${coreSrc}; return globalThis.WorkflowEditorCore;`)();
}

/** 载入画布脚本，返回 DOM 桩与 window.WorkflowEditor。 */
function boot({ definition, canWrite = true, agents = [] }) {
  const core = loadCore();
  const dom = createDom({
    state: {
      code: "demo",
      canWrite,
      agents,
      definition: definition || { entry: "", nodes: [], edges: [] },
    },
  });
  const factory = new Function("document", "window", "globalThis", editorSrc);
  factory(dom.document, dom.window, globalThis);
  return { core, dom, editor: dom.window.WorkflowEditor };
}

/** 在画布里按 class 找元素。 */
function findByClass(dom, className) {
  const hits = [];
  dom.canvas.walk((node) => {
    if (node.classList && node.classList.contains(className)) {
      hits.push(node);
    }
  });
  return hits;
}

/** 按 data-node-id 找节点卡片。 */
function findNodeGroup(dom, nodeId) {
  let hit = null;
  dom.canvas.walk((node) => {
    if (
      !hit &&
      node.getAttribute &&
      node.getAttribute("data-node-id") === nodeId
    ) {
      hit = node;
    }
  });
  return hit;
}

/** 在某个节点卡片里按 class 找元素。 */
function findInGroup(group, className) {
  let hit = null;
  group.walk((node) => {
    if (!hit && node.classList && node.classList.contains(className)) {
      hit = node;
    }
  });
  return hit;
}

/** 读隐藏 definition 字段并解析成对象。 */
function readDefinition(dom) {
  return JSON.parse(dom.definitionBox.value);
}

const baseDefinition = {
  entry: "step1",
  inputTemplate: "问：{{user_input}}",
  nodes: [
    { id: "step1", type: "agent", label: "识别", agentCode: "planner" },
    { id: "step2", type: "approval", prompt: "确认一下\n第二行" },
    { id: "step3", type: "agent" },
  ],
  edges: [{ from: "step1", to: "step2", when: "" }],
  layout: {
    nodes: {
      step1: { x: 40, y: 40 },
      step2: { x: 336, y: 40 },
      step3: { x: 40, y: 160 },
    },
    viewport: { x: 0, y: 0, scale: 1 },
  },
};

test("用例 1：渲染出节点、边、箭头、标题、徽章与入口标记", () => {
  const { dom } = boot({
    definition: baseDefinition,
    agents: [{ code: "planner", name: "规划器" }],
  });

  const nodes = findByClass(dom, "workflow-node");
  const edges = findByClass(dom, "workflow-edge");
  assert.equal(nodes.length, 3);
  assert.equal(edges.length, 1);

  const ids = nodes.map((node) => node.getAttribute("data-node-id"));
  assert.deepEqual(ids, ["step1", "step2", "step3"]);

  const path = edges[0].children[0];
  assert.equal(path.getAttribute("marker-end"), "url(#workflow-arrow)");

  const step1 = findNodeGroup(dom, "step1");
  assert.equal(findInGroup(step1, "title").textContent, "识别");
  assert.equal(findInGroup(step1, "badge").textContent, "Agent");
  assert.equal(findInGroup(step1, "subtitle").textContent, "规划器");

  const step2 = findNodeGroup(dom, "step2");
  assert.equal(findInGroup(step2, "title").textContent, "step2");
  assert.equal(findInGroup(step2, "badge").textContent, "审批");
  // 副标题只取 prompt 首行（设计文档 :205「副标题单行省略」）。
  assert.equal(findInGroup(step2, "subtitle").textContent, "确认一下");

  const step3 = findNodeGroup(dom, "step3");
  assert.equal(findInGroup(step3, "title").textContent, "step3");

  // 入口节点带入口标记，非入口节点不带。
  assert.equal(findByClass(dom, "entry-mark").length, 1);
  assert.ok(findInGroup(step1, "entry-mark"));
  assert.equal(findInGroup(step2, "entry-mark"), null);
  assert.equal(findInGroup(step3, "entry-mark"), null);
});

test("用例 2：初始渲染后隐藏 definition 字段是可解析 JSON", () => {
  const { dom } = boot({ definition: baseDefinition });

  const parsed = readDefinition(dom);
  assert.equal(parsed.nodes.length, 3);
  assert.equal(parsed.edges.length, 1);
  assert.equal(parsed.entry, "step1");
  assert.ok(parsed.layout);
  assert.ok(parsed.layout.nodes);
  assert.deepEqual(parsed.layout.viewport, { x: 0, y: 0, scale: 1 });
});

test("用例 3：节点拖拽按 8px 吸附，拖动结束才记一步撤销", () => {
  const { core, dom, editor } = boot({ definition: baseDefinition });
  const group = findNodeGroup(dom, "step1");

  // 在 step1 卡片内按下（卡片左上角 40,40，按在 50,50 处）。
  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 50,
    clientY: 50,
  });
  dom.canvas.dispatch("pointermove", { clientX: 137, clientY: 129 });

  const moved = editor.state.model.layout.nodes.step1;
  assert.equal(moved.x % core.GRID_SIZE, 0);
  assert.equal(moved.y % core.GRID_SIZE, 0);
  assert.deepEqual(moved, { x: 128, y: 120 });

  // 拖动过程中不记撤销。
  assert.equal(editor.state.stack.canUndo(), false);
  assert.equal(editor.state.stack.size(), 0);

  dom.canvas.dispatch("pointerup", { clientX: 137, clientY: 129 });

  // 拖动结束才记一步撤销。
  assert.equal(editor.state.stack.canUndo(), true);
  assert.equal(editor.state.stack.size(), 1);

  // 撤销回到拖动前的坐标。
  const undone = editor.state.stack.undo();
  assert.deepEqual(undone.layout.nodes.step1, { x: 40, y: 40 });

  // 隐藏字段里也是吸附后的坐标。
  assert.deepEqual(readDefinition(dom).layout.nodes.step1, { x: 128, y: 120 });
});

test("用例 4：只读用户拖不动节点", () => {
  const { dom, editor } = boot({ definition: baseDefinition, canWrite: false });
  const group = findNodeGroup(dom, "step1");

  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 50,
    clientY: 50,
  });
  dom.canvas.dispatch("pointermove", { clientX: 300, clientY: 300 });
  dom.canvas.dispatch("pointerup", { clientX: 300, clientY: 300 });

  assert.deepEqual(editor.state.model.layout.nodes.step1, { x: 40, y: 40 });
  assert.deepEqual(readDefinition(dom).layout.nodes.step1, { x: 40, y: 40 });
  assert.equal(editor.state.stack.canUndo(), false);
  assert.ok(dom.canvas.classList.contains("is-readonly"));
});

test("用例 5：中键拖空白处平移画布并写回 viewport", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  dom.canvas.dispatch("pointerdown", {
    target: dom.canvas,
    button: 1,
    clientX: 10,
    clientY: 20,
  });
  dom.canvas.dispatch("pointermove", { clientX: 110, clientY: 80 });
  dom.canvas.dispatch("pointerup", { clientX: 110, clientY: 80 });

  assert.deepEqual(editor.state.viewport, { x: 100, y: 60, scale: 1 });
  assert.deepEqual(editor.state.model.layout.viewport, {
    x: 100,
    y: 60,
    scale: 1,
  });
  assert.deepEqual(readDefinition(dom).layout.viewport, {
    x: 100,
    y: 60,
    scale: 1,
  });
  // 平移不记撤销。
  assert.equal(editor.state.stack.canUndo(), false);
});

test("用例 5b：空格加左键拖空白处同样平移", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  dom.document.dispatch("keydown", { code: "Space", key: " " });
  dom.canvas.dispatch("pointerdown", {
    target: dom.canvas,
    button: 0,
    clientX: 0,
    clientY: 0,
  });
  dom.canvas.dispatch("pointermove", { clientX: 30, clientY: 40 });
  dom.canvas.dispatch("pointerup", { clientX: 30, clientY: 40 });
  dom.document.dispatch("keyup", { code: "Space", key: " " });

  assert.deepEqual(editor.state.viewport, { x: 30, y: 40, scale: 1 });
  assert.deepEqual(readDefinition(dom).layout.viewport, {
    x: 30,
    y: 40,
    scale: 1,
  });
});

test("用例 6：滚轮以指针为中心缩放，比例夹在 0.5-2 之间", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  /** 指针下的画布坐标。 */
  function canvasPointAt(clientX, clientY) {
    const view = editor.state.viewport;
    return {
      x: (clientX - view.x) / view.scale,
      y: (clientY - view.y) / view.scale,
    };
  }

  const before = canvasPointAt(400, 300);
  dom.canvas.dispatch("wheel", {
    clientX: 400,
    clientY: 300,
    deltaY: -100,
  });
  const after = canvasPointAt(400, 300);
  assert.ok(Math.abs(after.x - before.x) < 1e-9);
  assert.ok(Math.abs(after.y - before.y) < 1e-9);
  assert.equal(editor.state.viewport.scale, 1.1);

  // 放大到上限 2。
  for (let i = 0; i < 30; i += 1) {
    dom.canvas.dispatch("wheel", { clientX: 400, clientY: 300, deltaY: -100 });
  }
  assert.equal(editor.state.viewport.scale, 2);

  // 缩小到下限 0.5。
  for (let i = 0; i < 40; i += 1) {
    dom.canvas.dispatch("wheel", { clientX: 400, clientY: 300, deltaY: 100 });
  }
  assert.equal(editor.state.viewport.scale, 0.5);

  // 缩放写回 definition。
  assert.equal(readDefinition(dom).layout.viewport.scale, 0.5);
});

test("用例 7：节点 id 为 __proto__ 时全链路不崩不丢", () => {
  const definition = {
    entry: "__proto__",
    inputTemplate: "",
    nodes: [
      { id: "__proto__", type: "agent", label: "原型节点" },
      { id: "normal", type: "approval", prompt: "确认" },
    ],
    edges: [{ from: "__proto__", to: "normal", when: "" }],
    layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
  };

  // ① 渲染不抛异常。
  const { dom, editor } = boot({ definition });
  assert.equal(findByClass(dom, "workflow-node").length, 2);
  const group = findNodeGroup(dom, "__proto__");
  assert.ok(group);
  assert.ok(!group.getAttribute("transform").includes("NaN"));

  // ② 拖动不抛异常。
  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 10,
    clientY: 10,
  });
  dom.canvas.dispatch("pointermove", { clientX: 100, clientY: 100 });
  dom.canvas.dispatch("pointerup", { clientX: 100, clientY: 100 });

  // ③ 坐标存得下，且是有限数字。
  const parsed = readDefinition(dom);
  const table = parsed.layout.nodes;
  assert.ok(Object.prototype.hasOwnProperty.call(table, "__proto__"));
  const point = table["__proto__"];
  assert.ok(Number.isFinite(point.x));
  assert.ok(Number.isFinite(point.y));
  assert.equal(point.x % 8, 0);
  assert.equal(point.y % 8, 0);

  // 渲染坐标不再是 NaN。
  const moved = findNodeGroup(dom, "__proto__");
  assert.ok(!moved.getAttribute("transform").includes("NaN"));
  assert.ok(editor.state.model.layout.nodes["__proto__"]);
});
