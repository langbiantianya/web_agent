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
/**
 * 载入画布脚本。
 *
 * 默认关掉实时校验：它是防抖计时器驱动的异步副作用，会让计时器活过用例
 * 边界并污染后续用例。需要验校验的用例自己打开，并在结尾调 cancelValidation()。
 */
function boot({
  definition,
  canWrite = true,
  agents = [],
  validation = false,
  lang = "zh-CN",
}) {
  const core = loadCore();
  const dom = createDom({
    lang,
    state: {
      code: "demo",
      canWrite,
      agents,
      definition: definition || { entry: "", nodes: [], edges: [] },
    },
  });
  const factory = new Function("document", "window", "globalThis", editorSrc);
  factory(dom.document, dom.window, globalThis);
  const editor = dom.window.WorkflowEditor;
  if (editor) {
    editor.state.validationEnabled = validation;
    editor.cancelValidation();
  }
  return { core, dom, editor };
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

/** 在某个根元素里按 data-field 找控件。 */
function findByField(host, name) {
  let hit = null;
  host.walk((node) => {
    if (!hit && node.getAttribute && node.getAttribute("data-field") === name) {
      hit = node;
    }
  });
  return hit;
}

/** 在某个根元素里按任意 data-* 属性找元素。 */
function findByData(host, attr, value) {
  let hit = null;
  host.walk((node) => {
    if (!hit && node.getAttribute && node.getAttribute(attr) === value) {
      hit = node;
    }
  });
  return hit;
}

/** 提示条里的文字。 */
function noticeTexts(dom) {
  return dom.errorList.children.map((item) => item.textContent);
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
  assert.equal(findInGroup(step1, "badge").textContent, "智能体");
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

/* ------------------------------------------------------------------
   Task 7：连线、选择与属性检查器
   ------------------------------------------------------------------ */

/** 调色板里某个类型的按钮。 */
function paletteButton(dom, type) {
  return dom.document
    .querySelectorAll(".workflow-palette-item")
    .find((button) => button.getAttribute("data-node-type") === type);
}

/** 从输出端口拖到目标节点（或画布空白）。 */
function dragPort(dom, fromId, targetId) {
  const group = findNodeGroup(dom, fromId);
  const port = findInGroup(group, "port-out");
  assert.ok(port, `节点 ${fromId} 应该有输出端口`);
  dom.canvas.dispatch("pointerdown", {
    target: port,
    clientX: 200,
    clientY: 40,
  });
  const target = targetId ? findNodeGroup(dom, targetId) : dom.canvas;
  dom.canvas.dispatch("pointerup", { target, clientX: 10, clientY: 10 });
}

test("用例 8：点调色板新增 agent 节点，id 不冲突且画布上多一个节点", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  paletteButton(dom, "agent").dispatch("click");

  assert.equal(editor.state.model.nodes.length, 4);
  const added = editor.state.model.nodes[3];
  assert.equal(added.type, "agent");
  assert.equal(added.id, "step4");
  // 新 id 不与现有节点冲突。
  assert.equal(
    new Set(editor.state.model.nodes.map((node) => node.id)).size,
    4,
  );
  // 画布上多一个节点卡片。
  assert.equal(findByClass(dom, "workflow-node").length, 4);
  assert.ok(findNodeGroup(dom, "step4"));
  // 撤销栈多一步。
  assert.equal(editor.state.stack.size(), 1);
  assert.equal(editor.state.stack.canUndo(), true);
  // 新节点被选中。
  assert.equal(editor.state.selection.kind, "node");
  assert.equal(editor.state.selection.id, "step4");
  // 坐标写进了布局表。
  assert.ok(editor.state.model.layout.nodes.step4);
});

test("用例 9：点调色板新增审批节点，默认会签人数与超时动作", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  paletteButton(dom, "approval").dispatch("click");

  const added = editor.state.model.nodes[3];
  assert.equal(added.type, "approval");
  assert.equal(added.id, "approval1");
  assert.equal(added.requiredApprovals, 1);
  assert.equal(added.approvalTimeoutAction, "reject");
  assert.equal(added.approvalTimeoutSec, 0);
  assert.equal(findByClass(dom, "workflow-node").length, 4);
});

test("用例 10：从输出端口拖到目标节点建立一条边", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  dragPort(dom, "step1", "step3");

  assert.equal(editor.state.model.edges.length, 2);
  assert.deepEqual(editor.state.model.edges[1], {
    from: "step1",
    to: "step3",
    when: "",
  });
  // 画布上多一条边。
  assert.equal(findByClass(dom, "workflow-edge").length, 2);
  // 撤销一步回到没有这条边的样子。
  const undone = editor.state.stack.undo();
  assert.equal(undone.edges.length, 1);
  assert.equal(undone.edges[0].from, "step1");
});

test("用例 11：自环被拒绝并给出提示", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  dragPort(dom, "step1", "step1");

  assert.equal(editor.state.model.edges.length, 1);
  assert.deepEqual(noticeTexts(dom), ["不能连到自己"]);
  assert.equal(findByClass(dom, "workflow-edge").length, 1);
  // 被拒绝的连线不记撤销。
  assert.equal(editor.state.stack.size(), 0);
});

test("用例 11b：英文界面里的画布动态提示使用英文", () => {
  const { dom } = boot({ definition: baseDefinition, lang: "en" });

  dragPort(dom, "step1", "step1");

  assert.deepEqual(noticeTexts(dom), ["Cannot connect a node to itself"]);
  assert.equal(
    findInGroup(findNodeGroup(dom, "step2"), "badge").textContent,
    "Approval",
  );
});

test("用例 12：重复边被拒绝并给出提示", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  dragPort(dom, "step1", "step2");

  assert.equal(editor.state.model.edges.length, 1);
  assert.deepEqual(noticeTexts(dom), ["这条边已经存在"]);
  assert.equal(findByClass(dom, "workflow-edge").length, 1);
  assert.equal(editor.state.stack.size(), 0);
});

test("用例 13：拖到空白处松手取消连线，也不提示", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  dragPort(dom, "step1", null);

  assert.equal(editor.state.model.edges.length, 1);
  assert.deepEqual(noticeTexts(dom), []);
  assert.equal(findByClass(dom, "workflow-edge").length, 1);
  assert.equal(editor.state.stack.size(), 0);
});

test("用例 14：改节点 id 是完整改名，边、入口与布局坐标一起跟着变", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  editor.select("node", "step1");
  const host = dom.inspector;
  const input = findByField(host, "id");
  assert.ok(input);
  input.value = "renamed";
  input.dispatch("change");

  const model = editor.state.model;
  assert.equal(model.nodes[0].id, "renamed");
  // 边的引用跟着变。
  assert.equal(model.edges[0].from, "renamed");
  assert.equal(model.edges[0].to, "step2");
  // 入口跟着变。
  assert.equal(model.entry, "renamed");
  // 布局坐标的键迁到新 id，旧键消失。
  assert.ok(Object.prototype.hasOwnProperty.call(model.layout.nodes, "renamed"));
  assert.ok(!Object.prototype.hasOwnProperty.call(model.layout.nodes, "step1"));
  assert.deepEqual(model.layout.nodes.renamed, { x: 40, y: 40 });
  // 选中态跟着变。
  assert.equal(editor.state.selection.id, "renamed");
  // 隐藏字段里也是改完的结果。
  const parsed = readDefinition(dom);
  assert.equal(parsed.entry, "renamed");
  assert.equal(parsed.edges[0].from, "renamed");
});

test("用例 14b：改成已被占用的 id 时不改并提示", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  editor.select("node", "step1");
  const input = findByField(dom.inspector, "id");
  input.value = "step2";
  input.dispatch("change");

  // id 没被改。
  assert.equal(editor.state.model.nodes[0].id, "step1");
  assert.equal(editor.state.model.nodes.length, 3);
  assert.ok(editor.state.model.nodes.some((node) => node.id === "step2"));
  // 有提示。
  assert.equal(noticeTexts(dom).length, 1);
  assert.ok(noticeTexts(dom)[0].includes("step2"));
});

test("用例 14c：改名成 __proto__ 时布局坐标仍然是自有属性", () => {
  const definition = {
    entry: "a",
    inputTemplate: "",
    nodes: [{ id: "a", type: "agent" }],
    edges: [],
    layout: {
      nodes: { a: { x: 8, y: 16 } },
      viewport: { x: 0, y: 0, scale: 1 },
    },
  };
  const { dom, editor } = boot({ definition });

  editor.select("node", "a");
  const input = findByField(dom.inspector, "id");
  input.value = "__proto__";
  input.dispatch("change");

  const nodes = editor.state.model.layout.nodes;
  assert.equal(editor.state.model.nodes[0].id, "__proto__");
  // 坐标必须是自有属性，不能被 __proto__ setter 吞进原型链。
  assert.ok(Object.prototype.hasOwnProperty.call(nodes, "__proto__"));
  assert.deepEqual(nodes["__proto__"], { x: 8, y: 16 });
});

test("用例 15：变量按钮插到光标处并补上分隔空格", () => {
  const definition = {
    entry: "a",
    inputTemplate: "",
    nodes: [
      { id: "a", type: "agent", outputKey: "plan" },
      { id: "b", type: "agent", outputKey: "my-key" },
    ],
    edges: [{ from: "a", to: "b", when: "" }],
    layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
  };
  const { dom, editor } = boot({ definition });

  // 通过点击画布上的边来选中，不依赖内部的边键格式。
  findByClass(dom, "workflow-edge")[0].dispatch("click");
  const host = dom.inspector;
  const when = findByField(host, "when");
  assert.ok(when);
  when.value = "user_input";
  when.selectionStart = 10;
  when.selectionEnd = 10;

  const button = findByData(host, "data-variable", "upstream.plan");
  assert.ok(button);
  button.dispatch("click");

  // 插在光标处，不在末尾。左边已经是完整操作数，所以补的是 ` and ` 而不是
  // 裸空格 —— `user_input upstream.plan` 会被服务端判成非法表达式。
  assert.equal(when.value, "user_input and upstream.plan");

  // 不合法的 outputKey 不出现在按钮列表里。
  assert.equal(findByData(host, "data-variable", "upstream.my-key"), null);
  assert.equal(findByData(host, "data-variable", "my-key"), null);
  // user_input 始终有按钮。
  assert.ok(findByData(host, "data-variable", "user_input"));

  // 变量按钮点完不立刻提交，失焦（change）才提交。
  assert.equal(editor.state.model.edges[0].when, "");
  when.dispatch("change");
  assert.equal(
    editor.state.model.edges[0].when,
    "user_input and upstream.plan",
  );
});

test("用例 15b：变量插到文本中间而不是末尾", () => {
  const definition = {
    entry: "a",
    inputTemplate: "",
    nodes: [
      { id: "a", type: "agent", outputKey: "plan" },
      { id: "b", type: "agent" },
    ],
    edges: [{ from: "a", to: "b", when: "" }],
    layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
  };
  const { dom, editor } = boot({ definition });

  // 通过点击画布上的边来选中，不依赖内部的边键格式。
  findByClass(dom, "workflow-edge")[0].dispatch("click");
  const host = dom.inspector;
  const when = findByField(host, "when");
  when.value = "user_input and tail";
  when.selectionStart = 10;
  when.selectionEnd = 10;

  findByData(host, "data-variable", "upstream.plan").dispatch("click");

  // 插在光标处（"user_input" 之后），左边是完整操作数 → 补 ` and `。
  assert.equal(when.value, "user_input and upstream.plan and tail");
});

test("用例 16：改字段只更新 model，检查器 DOM 不被重建", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  editor.select("node", "step1");
  const host = dom.inspector;
  const input = findByField(host, "label");
  assert.ok(input);
  const before = input;
  const builds = editor.state.inspectorBuilds;

  input.value = "新的标题";
  input.dispatch("change");

  // model 变了。
  assert.equal(editor.state.model.nodes[0].label, "新的标题");
  assert.equal(readDefinition(dom).nodes[0].label, "新的标题");
  // 检查器没有被重建：还是同一个元素引用。
  assert.equal(findByField(host, "label"), before);
  assert.equal(editor.state.inspectorBuilds, builds);
});

test("用例 17：切换节点类型会清掉互斥字段", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  editor.select("node", "step1");
  const typeSelect = findByField(dom.inspector, "type");
  typeSelect.value = "approval";
  typeSelect.dispatch("change");

  const node = editor.state.model.nodes[0];
  assert.equal(node.type, "approval");
  // agent 专属字段被清掉。
  assert.equal(node.agentCode, "");
  // 审批字段回到默认值。
  assert.equal(node.requiredApprovals, 1);
  assert.equal(node.approvalTimeoutAction, "reject");
  assert.equal(node.approvalTimeoutSec, 0);
});

test("用例 18：Delete 键删除选中的节点与边", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  editor.select("node", "step3");
  dom.document.dispatch("keydown", { key: "Delete" });
  assert.equal(editor.state.model.nodes.length, 2);
  assert.equal(editor.state.model.edges.length, 1);
  assert.equal(editor.state.selection, null);

  // 通过点击画布上的边来选中，不依赖内部的边键格式。
  findByClass(dom, "workflow-edge")[0].dispatch("click");
  dom.document.dispatch("keydown", { key: "Backspace" });
  assert.equal(editor.state.model.edges.length, 0);
  assert.equal(editor.state.selection, null);
});

test("用例 19：只读用户不能新增节点、不能连边、控件全部禁用", () => {
  const { dom, editor } = boot({ definition: baseDefinition, canWrite: false });

  paletteButton(dom, "agent").dispatch("click");
  assert.equal(editor.state.model.nodes.length, 3);

  dragPort(dom, "step1", "step3");
  assert.equal(editor.state.model.edges.length, 1);

  editor.select("node", "step1");
  const input = findByField(dom.inspector, "id");
  // 只读时控件禁用。
  assert.equal(input.disabled, true);
  const typeSelect = findByField(dom.inspector, "type");
  assert.equal(typeSelect.disabled, true);
});

test("用例 20：未选中对象时检查器给出画布表单（入口与输入模板）", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  editor.select(null, null);
  const entry = findByField(dom.inspector, "entry");
  const template = findByField(dom.inspector, "inputTemplate");
  assert.ok(entry);
  assert.ok(template);
  assert.equal(template.value, "问：{{user_input}}");

  entry.value = "step2";
  entry.dispatch("change");
  assert.equal(editor.state.model.entry, "step2");
  assert.equal(readDefinition(dom).entry, "step2");

  template.value = "新模板 {{user_input}}";
  template.dispatch("change");
  assert.equal(editor.state.model.inputTemplate, "新模板 {{user_input}}");
});

/* ------------------------------------------------------------------
   Task 6 审阅整改（2 个 Important + 5 个 Minor）
   ------------------------------------------------------------------ */

test("用例 21：布局表是普通对象时，拖动 __proto__ 节点仍写出自有属性", () => {
  const definition = {
    entry: "__proto__",
    inputTemplate: "",
    nodes: [{ id: "__proto__", type: "agent", label: "原型节点" }],
    edges: [],
    layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
  };
  const { core, dom, editor } = boot({ definition });

  // 先提交一份克隆体：cloneModel 走 JSON 路径，
  // layout.nodes 从无原型对象变成**普通对象**（带 Object.prototype），
  // 普通赋值会触发 __proto__ setter —— 这才是真正的防线场景。
  editor.commit(core.cloneModel(editor.state.model));
  const table = editor.state.model.layout.nodes;
  assert.equal(Object.getPrototypeOf(table), Object.prototype);
  assert.equal(
    Object.prototype.hasOwnProperty.call(table, "__proto__"),
    false,
  );

  const group = findNodeGroup(dom, "__proto__");
  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 10,
    clientY: 10,
  });
  dom.canvas.dispatch("pointermove", { clientX: 100, clientY: 100 });
  dom.canvas.dispatch("pointerup", { clientX: 100, clientY: 100 });

  // 坐标必须是**自有属性**，不能被 __proto__ setter 吞进原型链。
  assert.equal(Object.prototype.hasOwnProperty.call(table, "__proto__"), true);
  const point = table["__proto__"];
  assert.ok(Number.isFinite(point.x));
  assert.ok(Number.isFinite(point.y));
  assert.equal(point.x % 8, 0);
  assert.equal(point.y % 8, 0);
});

test("用例 22：指针落在节点内部的子元素上也能选中并拖动", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  const group = findNodeGroup(dom, "step1");
  // 真实浏览器里 pointerdown 的 target 是最深子元素（rect / text）。
  const title = findInGroup(group, "title");
  assert.ok(title);

  dom.canvas.dispatch("pointerdown", {
    target: title,
    clientX: 50,
    clientY: 50,
  });
  dom.canvas.dispatch("pointermove", { clientX: 137, clientY: 129 });
  dom.canvas.dispatch("pointerup", { clientX: 137, clientY: 129 });

  // 节点被选中了。
  assert.equal(editor.state.selection.kind, "node");
  assert.equal(editor.state.selection.id, "step1");
  // 坐标确实变了（8px 吸附）。
  assert.deepEqual(editor.state.model.layout.nodes.step1, { x: 128, y: 120 });
});

test("用例 23：重绘时画布被清空，节点卡片不累加", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  assert.equal(findByClass(dom, "workflow-node").length, 3);
  assert.equal(findByClass(dom, "workflow-edge").length, 1);

  editor.render();
  assert.equal(findByClass(dom, "workflow-node").length, 3);
  assert.equal(findByClass(dom, "workflow-edge").length, 1);

  editor.render();
  assert.equal(findByClass(dom, "workflow-node").length, 3);
  assert.equal(findByClass(dom, "workflow-edge").length, 1);
});

test("用例 24：超长标题与副标题被截断并以省略号结尾", () => {
  const long = "一".repeat(40);
  const { dom } = boot({
    definition: {
      entry: "a",
      inputTemplate: "",
      nodes: [
        { id: "a", type: "agent", label: long },
        { id: "b", type: "approval", prompt: long },
      ],
      edges: [],
      layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
    },
  });

  const title = findInGroup(findNodeGroup(dom, "a"), "title").textContent;
  assert.ok(title.endsWith("…"));
  assert.ok(title.length <= 18);

  const subtitle = findInGroup(findNodeGroup(dom, "b"), "subtitle")
    .textContent;
  assert.ok(subtitle.endsWith("…"));
  assert.ok(subtitle.length <= 20);
});

test("用例 25：只点一下不移动不记撤销", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  const group = findNodeGroup(dom, "step1");

  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 50,
    clientY: 50,
  });
  dom.canvas.dispatch("pointerup", { clientX: 50, clientY: 50 });

  // 没挪动 → 不记一步撤销（否则用户按 Ctrl+Z 会没反应）。
  assert.equal(editor.state.stack.size(), 0);
  assert.equal(editor.state.stack.canUndo(), false);
  assert.deepEqual(editor.state.model.layout.nodes.step1, { x: 40, y: 40 });

  // 真的拖动了才记一步。
  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 50,
    clientY: 50,
  });
  dom.canvas.dispatch("pointermove", { clientX: 137, clientY: 129 });
  dom.canvas.dispatch("pointerup", { clientX: 137, clientY: 129 });
  assert.equal(editor.state.stack.size(), 1);
  assert.deepEqual(editor.state.model.layout.nodes.step1, { x: 128, y: 120 });
});

test("用例 26：pointercancel 不记撤销、不写脏标记并还原坐标", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  const group = findNodeGroup(dom, "step1");

  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 50,
    clientY: 50,
  });
  dom.canvas.dispatch("pointermove", { clientX: 137, clientY: 129 });
  dom.canvas.dispatch("pointercancel", { clientX: 137, clientY: 129 });

  assert.equal(editor.state.stack.size(), 0);
  assert.equal(editor.state.dirty, false);
  // 取消就是取消：坐标回到起点。
  assert.deepEqual(editor.state.model.layout.nodes.step1, { x: 40, y: 40 });
});

test("用例 27：在输入框里按空格不会让画布进入平移态", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  editor.select("node", "step1");
  const input = findByField(dom.inspector, "label");

  dom.document.dispatch("keydown", {
    target: input,
    code: "Space",
    key: " ",
  });
  dom.canvas.dispatch("pointerdown", {
    target: dom.canvas,
    button: 0,
    clientX: 0,
    clientY: 0,
  });
  dom.canvas.dispatch("pointermove", { clientX: 30, clientY: 40 });
  dom.canvas.dispatch("pointerup", { clientX: 30, clientY: 40 });

  // 空格被输入框吃掉，画布没有平移。
  assert.deepEqual(editor.state.viewport, { x: 0, y: 0, scale: 1 });

  dom.document.dispatch("keyup", {
    target: input,
    code: "Space",
    key: " ",
  });
});

test("用例 28：画布矩形有偏移时，拖拽与缩放都按偏移算坐标", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  // 画布不是贴着窗口左上角的：矩形有非零偏移。
  dom.canvas.rect = { left: 25, top: 15, width: 800, height: 600 };

  // 拖拽：相对位移不受画布偏移影响，但结果必须落在 8px 网格上。
  const group = findNodeGroup(dom, "step1");
  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: 75,
    clientY: 55,
  });
  dom.canvas.dispatch("pointermove", { clientX: 162, clientY: 134 });
  dom.canvas.dispatch("pointerup", { clientX: 162, clientY: 134 });
  const moved = editor.state.model.layout.nodes.step1;
  assert.equal(moved.x % 8, 0);
  assert.equal(moved.y % 8, 0);
  assert.deepEqual(moved, { x: 128, y: 120 });

  // 缩放：以指针为中心，指针下的画布坐标不变。
  const view = editor.state.viewport;
  const before = {
    x: (400 - 25 - view.x) / view.scale,
    y: (300 - 15 - view.y) / view.scale,
  };
  dom.canvas.dispatch("wheel", {
    clientX: 400,
    clientY: 300,
    deltaY: -100,
  });
  const after = {
    x: (400 - 25 - editor.state.viewport.x) / editor.state.viewport.scale,
    y: (300 - 15 - editor.state.viewport.y) / editor.state.viewport.scale,
  };
  assert.ok(Math.abs(after.x - before.x) < 1e-9);
  assert.ok(Math.abs(after.y - before.y) < 1e-9);
  // 偏移必须真的参与了计算：去掉 rect.left 就会得到别的值。
  assert.ok(Math.abs(editor.state.viewport.x - -37.5) < 1e-9);
  assert.ok(Math.abs(editor.state.viewport.y - -28.5) < 1e-9);
});

/* ==================================================================
   Task 8 新增用例：撤销重做、布局、适应画布、实时校验、JSON 预览
   ================================================================== */

/**
 * 装上可控的假 fetch，返回指定响应。
 * 调用方负责在 finally 里 restore()，别把别的用例污染了。
 */
function stubFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return handler(url, options);
  };
  return {
    calls,
    restore() {
      globalThis.fetch = original;
    },
  };
}

/** 造一个最小 Response 形状（只要 ok / status / json 三项）。 */
function fakeResponse(body, ok = true, status = 200) {
  return {
    ok,
    status,
    async json() {
      return body;
    },
  };
}

/** 从节点组的 transform 里取出它当前落在画布上的坐标。 */
function nodeOrigin(dom, nodeId) {
  const group = findNodeGroup(dom, nodeId);
  assert.ok(group, `找不到节点 ${nodeId}`);
  const match = /translate\(([-\d.]+),\s*([-\d.]+)\)/.exec(
    String(group.getAttribute("transform") || ""),
  );
  assert.ok(match, `节点 ${nodeId} 的 transform 不可解析`);
  return { x: Number(match[1]), y: Number(match[2]) };
}

/** 按当前选中键反查边（用编辑器导出的稳定键，不依赖内部实现）。 */
function findEdgeBySelection(editor) {
  if (!editor.state.selection || editor.state.selection.kind !== "edge") {
    return null;
  }
  return (
    editor.state.model.edges.find(
      (edge) => editor.edgeKey(edge) === editor.state.selection.id,
    ) || null
  );
}

test("用例 29：撤销与重做按钮，以及 disabled 状态随栈变化", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  // 初始没有可撤销的内容。
  assert.equal(dom.toolbar.undo.disabled, true);
  assert.equal(dom.toolbar.redo.disabled, true);

  const before = editor.state.model.nodes.length;
  paletteButton(dom, "agent").dispatch("click");
  assert.equal(editor.state.model.nodes.length, before + 1);
  assert.equal(dom.toolbar.undo.disabled, false);

  dom.toolbar.undo.dispatch("click");
  assert.equal(editor.state.model.nodes.length, before);
  assert.equal(dom.toolbar.redo.disabled, false);

  dom.toolbar.redo.dispatch("click");
  assert.equal(editor.state.model.nodes.length, before + 1);
});

test("用例 30：Ctrl+Z 撤销 / Ctrl+Shift+Z 重做，输入框里不抢", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  const before = editor.state.model.nodes.length;

  paletteButton(dom, "agent").dispatch("click");
  assert.equal(editor.state.model.nodes.length, before + 1);

  dom.document.dispatch("keydown", { key: "z", ctrlKey: true });
  assert.equal(editor.state.model.nodes.length, before);

  dom.document.dispatch("keydown", {
    key: "z",
    ctrlKey: true,
    shiftKey: true,
  });
  assert.equal(editor.state.model.nodes.length, before + 1);

  // 焦点在输入框里时不触发画布撤销。
  const stopped = editor.state.model.nodes.length;
  const input = dom.document.createElement("input");
  dom.document.dispatch("keydown", {
    key: "z",
    ctrlKey: true,
    target: input,
  });
  assert.equal(editor.state.model.nodes.length, stopped);
});

test("用例 31：整理布局按层级重排，重复点击结果一致且不重置视角", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  editor.state.viewport = { x: 123, y: 45, scale: 1.25 };
  editor.state.model.layout.viewport = { x: 123, y: 45, scale: 1.25 };

  dom.toolbar.layout.dispatch("click");
  const first = JSON.parse(JSON.stringify(editor.state.model.layout.nodes));
  // 层轴是 x：step2 在 step1 的右边。
  assert.ok(first.step2.x > first.step1.x);
  // 视角没被 computeLayout 返回的 {0,0,1} 覆盖。
  const viewport = readDefinition(dom).layout.viewport;
  assert.equal(viewport.scale, 1.25);
  assert.equal(viewport.x, 123);

  dom.toolbar.layout.dispatch("click");
  const second = JSON.parse(JSON.stringify(editor.state.model.layout.nodes));
  assert.deepEqual(second, first);
});

test("用例 32：适应画布只改视口，不改节点坐标", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  const beforeNodes = JSON.parse(
    JSON.stringify(editor.state.model.layout.nodes),
  );
  editor.state.viewport = { x: 9999, y: 9999, scale: 2 };

  dom.toolbar.fit.dispatch("click");

  // 节点坐标一个都没动（layout.nodes 是无原型对象，用 JSON 比内容）。
  assert.equal(
    JSON.stringify(editor.state.model.layout.nodes),
    JSON.stringify(beforeNodes),
  );
  // 视口被重新算过，并写回了 definition。
  const viewport = readDefinition(dom).layout.viewport;
  assert.ok(viewport.scale >= 0.5 && viewport.scale <= 2);
  assert.notEqual(viewport.x, 9999);
  // 第一个节点应当落在可视区域内。
  const origin = nodeOrigin(dom, "step1");
  assert.ok(origin.x * viewport.scale + viewport.x >= 0);
});

test("用例 33：实时校验成功时不显示错误条", async () => {
  const { dom, editor } = boot({ definition: baseDefinition, validation: true });
  const fake = stubFetch(() => fakeResponse({ ok: true }));
  try {
    await editor.runValidation();
    assert.equal(dom.errorBar.getAttribute("hidden"), "hidden");
    assert.equal(editor.state.issues.length, 0);
    assert.equal(fake.calls.length, 1);
    // 请求体里带 csrf_token 与 definition。
    const body = JSON.parse(fake.calls[0].options.body);
    assert.equal(body.csrf_token, "csrf-demo");
    assert.ok(body.definition && Array.isArray(body.definition.nodes));
    // 打的是校验端点。
    assert.equal(fake.calls[0].url, "/admin/workflows/validate");
  } finally {
    fake.restore();
    editor.cancelValidation();
  }
});

test("用例 34：实时校验失败时显示全部问题，并高亮节点与边", async () => {
  const { dom, editor } = boot({ definition: baseDefinition, validation: true });
  const fake = stubFetch(() =>
    fakeResponse({
      ok: false,
      errors: [
        { message: "definition 存在环", node: "step1" },
        { message: "孤立节点：step3", node: "step3" },
        { message: "when 有语法错误", node: "step1", edge: "step1->step2" },
      ],
    }),
  );
  try {
    await editor.runValidation();

    // 错误条显示三条（验收 11：全部问题都要报）。
    assert.equal(dom.errorList.children.length, 3);
    assert.equal(dom.errorList.children[0].textContent, "definition 存在环");
    assert.equal(dom.errorBar.getAttribute("hidden"), null);

    // 出问题的节点被标红，没问题的节点不标。
    assert.ok(findNodeGroup(dom, "step1").classList.contains("is-error"));
    assert.ok(findNodeGroup(dom, "step3").classList.contains("is-error"));
    assert.equal(
      findNodeGroup(dom, "step2").classList.contains("is-error"),
      false,
    );
    // 出问题的边也被标红。
    assert.ok(
      findByClass(dom, "workflow-edge")[0].classList.contains("is-error"),
    );
  } finally {
    fake.restore();
    editor.cancelValidation();
  }
});

test("用例 35：400/403 的机器码被翻成中文", async () => {
  const { dom, editor } = boot({ definition: baseDefinition, validation: true });
  const fake = stubFetch(() =>
    fakeResponse(
      { ok: false, errors: [{ message: "definition_shape_invalid" }] },
      false,
      400,
    ),
  );
  try {
    await editor.runValidation();
    assert.equal(dom.errorList.children.length, 1);
    assert.equal(
      dom.errorList.children[0].textContent,
      "工作流定义结构不正确",
    );
    // 不能把机器码原样显示出来。
    assert.equal(
      String(dom.errorList.children[0].textContent).includes(
        "definition_shape",
      ),
      false,
    );
  } finally {
    fake.restore();
    editor.cancelValidation();
  }
});

test("用例 36：校验请求失败不阻塞编辑，错误条收起", async () => {
  const { dom, editor } = boot({ definition: baseDefinition, validation: true });
  const fake = stubFetch(() => {
    throw new Error("network down");
  });
  try {
    await editor.runValidation();
    assert.equal(editor.state.issues.length, 0);
    assert.equal(dom.errorBar.getAttribute("hidden"), "hidden");
    // 编辑仍然可用。
    paletteButton(dom, "agent").dispatch("click");
    assert.ok(editor.state.model.nodes.length > baseDefinition.nodes.length);
  } finally {
    fake.restore();
    editor.cancelValidation();
  }
});

test("用例 37：JSON 预览展开时刷新成当前定义", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  // 先改一点东西，让当前定义与页面初始的不同。
  paletteButton(dom, "agent").dispatch("click");
  const nodeCount = editor.state.model.nodes.length;

  dom.toolbar["json-toggle"].dispatch("click");
  assert.equal(dom.jsonPanel.hidden, false);
  const shown = JSON.parse(dom.jsonView.textContent);
  assert.equal(shown.nodes.length, nodeCount);

  dom.toolbar["json-toggle"].dispatch("click");
  assert.equal(dom.jsonPanel.hidden, true);
});

test("用例 38：撤销之后仍然选中的是同一条边（稳定键）", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  // 再连一条边，让边数发生变化。
  dragPort(dom, "step2", "step3");
  assert.equal(editor.state.model.edges.length, 2);

  // 点选第一条边（step1->step2）。
  findByClass(dom, "workflow-edge")[0].dispatch("click");
  assert.equal(editor.state.selection.kind, "edge");

  // 撤销一步：刚连的那条边消失，边数回到 1。
  dom.toolbar.undo.dispatch("click");
  assert.equal(editor.state.model.edges.length, 1);

  // 选中的仍然是 step1->step2 那条，而不是被序号漂移带到别的边。
  const selected = findEdgeBySelection(editor);
  assert.ok(selected, "撤销之后选中的边应当还能定位到");
  assert.equal(selected.from, "step1");
  assert.equal(selected.to, "step2");
  assert.ok(findByField(dom.inspector, "when"));
});

test("用例 39：只点不拖不记撤销，拖回原点也不留坐标", () => {
  const { dom, editor } = boot({
    definition: {
      entry: "a",
      inputTemplate: "",
      // a 没有 layout 坐标 → 用兜底位置渲染。
      nodes: [{ id: "a", type: "agent", agentCode: "p" }],
      edges: [],
      layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
    },
  });
  const sizeBefore = editor.state.stack.size();
  const group = findNodeGroup(dom, "a");
  assert.ok(group);
  const start = nodeOrigin(dom, "a");

  // 拖起来又落回原处：中间必须真的有 pointermove，
  // 否则坐标压根没被写过，这条用例就测不到「清理」。
  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: start.x + 20,
    clientY: start.y + 20,
    pointerId: 1,
  });
  dom.canvas.dispatch("pointermove", {
    target: group,
    clientX: start.x + 20,
    clientY: start.y + 20,
    pointerId: 1,
  });
  // 拖拽过程中坐标确实被写进去了（证明真有东西要清理）。
  assert.equal(
    Object.prototype.hasOwnProperty.call(
      editor.state.model.layout.nodes,
      "a",
    ),
    true,
  );
  dom.canvas.dispatch("pointerup", {
    target: group,
    clientX: start.x + 20,
    clientY: start.y + 20,
    pointerId: 1,
  });

  // 回到原点 → 不记撤销，且拖拽期间写进去的坐标被撤掉。
  assert.equal(editor.state.stack.size(), sizeBefore);
  const definition = readDefinition(dom);
  assert.equal(
    Object.prototype.hasOwnProperty.call(definition.layout.nodes, "a"),
    false,
  );
});

test("用例 39b：只点一下不移动，什么都不改", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  const sizeBefore = editor.state.stack.size();
  const group = findNodeGroup(dom, "step1");
  const start = nodeOrigin(dom, "step1");

  dom.canvas.dispatch("pointerdown", {
    target: group,
    clientX: start.x,
    clientY: start.y,
    pointerId: 1,
  });
  dom.canvas.dispatch("pointerup", {
    target: group,
    clientX: start.x,
    clientY: start.y,
    pointerId: 1,
  });

  assert.equal(editor.state.stack.size(), sizeBefore);
  // 原本就有坐标，值也不能变。
  const point = readDefinition(dom).layout.nodes.step1;
  assert.equal(point.x, 40);
  assert.equal(point.y, 40);
});

test("用例 40：画布检查器的变量提示列出 upstream", () => {
  const { dom, editor } = boot({ definition: baseDefinition });
  editor.select(null, null);
  // 提示在检查器面板里，不在画布里。
  const hints = [];
  dom.inspector.walk((node) => {
    if (node.classList && node.classList.contains("workflow-inspector-hint")) {
      hints.push(node);
    }
  });
  assert.ok(hints.length >= 1, "画布检查器里应当有变量提示");
  const text = String(hints[hints.length - 1].textContent);
  // 必须出现在**变量清单**里，不能靠尾巴那句「不写 upstream. 前缀」蒙混。
  const list = text.split("（")[0];
  assert.ok(list.includes("user_input"), `变量清单里应当有 user_input：${list}`);
  assert.ok(list.includes("upstream"), `变量清单里应当有 upstream：${list}`);
});

test("用例 41：边标识与边在数组里的位置无关", () => {
  const { editor } = boot({ definition: baseDefinition });
  const extra = { from: "step3", to: "step1", when: "" };
  const model = editor.state.model;

  // 同一条边排第 0 位与排第 1 位，键必须完全一样。
  model.edges = [model.edges[0], extra];
  const head = editor.edgeKey(model.edges[0]);
  model.edges = [extra, model.edges[0]];
  const tail = editor.edgeKey(model.edges[1]);
  assert.equal(tail, head, "边键不能随数组下标变化");

  // 不同的端点对不能撞成同一个键。
  assert.notEqual(editor.edgeKey(model.edges[0]), editor.edgeKey(model.edges[1]));

  // 节点 id 里含 "->" 也不会让两对不同的端点撞键。
  const tricky = { from: "a->b", to: "c", when: "" };
  const plain = { from: "a", to: "b->c", when: "" };
  assert.notEqual(editor.edgeKey(tricky), editor.edgeKey(plain));
});

test("用例 42：撤销把选中的边删掉后，选中态被清空", () => {
  const { dom, editor } = boot({ definition: baseDefinition });

  // 先连一条边（step2->step3），再选中它。
  dragPort(dom, "step2", "step3");
  assert.equal(editor.state.model.edges.length, 2);
  const groups = findByClass(dom, "workflow-edge");
  groups[groups.length - 1].dispatch("click");
  assert.equal(editor.state.selection.kind, "edge");
  const selected = findEdgeBySelection(editor);
  assert.equal(selected.from, "step2");
  assert.equal(selected.to, "step3");

  // 撤销：这条边消失，选中态不能再指向一条不存在的边。
  dom.toolbar.undo.dispatch("click");
  assert.equal(editor.state.model.edges.length, 1);
  assert.equal(editor.state.selection, null);
  // 检查器回落到画布表单，而不是显示一条已不存在的边。
  assert.equal(findByField(dom.inspector, "when"), null);
});

test("用例 43：commit 之后会排队一次实时校验", async () => {
  const { dom, editor } = boot({ definition: baseDefinition, validation: true });
  const fake = stubFetch(() => fakeResponse({ ok: true }));
  try {
    editor.state.validationDelayMs = 0;
    assert.equal(fake.calls.length, 0);

    // 改动模型 → commit → 应当自动排队校验。
    paletteButton(dom, "agent").dispatch("click");
    await new Promise((resolve) => setTimeout(resolve, 20));

    assert.equal(fake.calls.length, 1, "commit 之后应当自动校验一次");
    assert.equal(fake.calls[0].url, "/admin/workflows/validate");
  } finally {
    fake.restore();
    editor.cancelValidation();
  }
});

// ------------------------------------------------- 全角文本的宽度截断

/** 估算一段文本占几个 em：全角 1，其余按 0.55。测试侧独立实现，不复用产品代码。 */
function estimateEm(text) {
  let total = 0;
  for (const char of text) {
    const code = char.codePointAt(0);
    const wide =
      (code >= 0x2e80 && code <= 0xa4cf) ||
      (code >= 0xac00 && code <= 0xd7a3) ||
      (code >= 0xf900 && code <= 0xfaff) ||
      (code >= 0xff00 && code <= 0xff60) ||
      (code >= 0x1f300 && code <= 0x1faff);
    total += wide ? 1 : 0.55;
  }
  return total;
}

test("用例 49：中文标题按显示宽度截断，不撑破卡片", () => {
  const long = "一".repeat(40);
  const { dom } = boot({
    definition: {
      entry: "a",
      inputTemplate: "",
      nodes: [
        { id: "a", type: "agent", label: long },
        { id: "b", type: "approval", prompt: long },
      ],
      edges: [],
      layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
    },
  });

  // 卡片正文可用宽度约 176px，标题字号 13px → 13em；省略号也要占位。
  const title = findInGroup(findNodeGroup(dom, "a"), "title").textContent;
  assert.ok(title.endsWith("…"), `应当以省略号结尾：${title}`);
  assert.equal(title, `${"一".repeat(12)}…`);
  assert.ok(
    estimateEm(title) <= 13,
    `标题估算宽度 ${estimateEm(title)}em 超出卡片可用宽度`,
  );

  const subtitle = findInGroup(findNodeGroup(dom, "b"), "subtitle")
    .textContent;
  assert.equal(subtitle, `${"一".repeat(14)}…`);
  assert.ok(
    estimateEm(subtitle) <= 15,
    `副标题估算宽度 ${estimateEm(subtitle)}em 超出卡片可用宽度`,
  );
});

test("用例 50：英文标题的截断行为不变（仍是 18 / 20 字）", () => {
  const long = "a".repeat(40);
  const { dom } = boot({
    definition: {
      entry: "a",
      inputTemplate: "",
      nodes: [
        { id: "a", type: "agent", label: long },
        { id: "b", type: "approval", prompt: "b".repeat(40) },
      ],
      edges: [],
      layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
    },
  });

  const title = findInGroup(findNodeGroup(dom, "a"), "title").textContent;
  assert.equal(title, `${"a".repeat(17)}…`);
  assert.equal(title.length, 18);

  const subtitle = findInGroup(findNodeGroup(dom, "b"), "subtitle")
    .textContent;
  assert.equal(subtitle, `${"b".repeat(19)}…`);
  assert.equal(subtitle.length, 20);
});

test("用例 52：emoji 与符号按 1em 计宽，没超限就不该加省略号", () => {
  // 每个 emoji 是 2 个 UTF-16 码元、1 个码点、1em 宽。
  // 10 个 = 20 码元，按码元数会误判成「超过 18 字」；按码点与宽度都在限内。
  const emoji = "😀".repeat(10);
  const watch = "⌚".repeat(10);
  const star = "⭐".repeat(10);
  const long = "😀".repeat(20);
  const { dom } = boot({
    definition: {
      entry: "a",
      inputTemplate: "",
      nodes: [
        { id: "a", type: "agent", label: emoji },
        { id: "b", type: "agent", label: watch },
        { id: "c", type: "agent", label: star },
        { id: "d", type: "agent", label: long },
      ],
      edges: [],
      layout: { nodes: {}, viewport: { x: 0, y: 0, scale: 1 } },
    },
  });

  const title = (id) =>
    findInGroup(findNodeGroup(dom, id), "title").textContent;
  assert.equal(title("a"), emoji, "10 个 emoji 没有超限，不该被截");
  assert.equal(title("b"), watch, "⌚ 按 emoji 呈现，宽度是 1em");
  assert.equal(title("c"), star, "⭐ 同上");
  // 超限时按码点截，不会劈开代理对。
  assert.equal(title("d"), `${"😀".repeat(12)}…`);
});

// ------------------------------------------------- 整组定位（环）

const cycleDefinition = {
  entry: "a",
  inputTemplate: "",
  nodes: [
    { id: "a", type: "agent", agentCode: "planner" },
    { id: "b", type: "agent", agentCode: "planner" },
    { id: "c", type: "agent", agentCode: "planner" },
  ],
  edges: [
    { from: "a", to: "b" },
    { from: "b", to: "c" },
    { from: "c", to: "a" },
  ],
  layout: {
    nodes: {
      a: { x: 40, y: 40 },
      b: { x: 336, y: 40 },
      c: { x: 632, y: 40 },
    },
    viewport: { x: 0, y: 0, scale: 1 },
  },
};

test("用例 51：成环时环上的每个节点与每条边都标红", async () => {
  const { dom, editor } = boot({ definition: cycleDefinition, validation: true });
  const fake = stubFetch(() =>
    fakeResponse({
      ok: false,
      errors: [
        {
          message: "definition 存在环，无法确定执行顺序",
          node: "a",
          nodes: ["a", "b", "c"],
          edge: "a->b",
          edges: ["a->b", "b->c", "c->a"],
        },
      ],
    }),
  );
  try {
    await editor.runValidation();

    for (const id of ["a", "b", "c"]) {
      assert.ok(
        findNodeGroup(dom, id).classList.contains("is-error"),
        `环上的节点 ${id} 应当被标红`,
      );
    }
    const marked = findByClass(dom, "workflow-edge").filter((edge) =>
      edge.classList.contains("is-error"),
    );
    assert.equal(marked.length, 3, "环上的三条边都应当被标红");
  } finally {
    fake.restore();
    editor.cancelValidation();
  }
});
