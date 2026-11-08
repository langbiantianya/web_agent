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
  const state = core.toModel({
    nodes: [{ id: "a", type: "agent", agentCode: "p" }],
    edges: [],
  });
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
  const base = core.toModel({
    nodes: [{ id: "a", type: "agent", agentCode: "p" }],
    edges: [],
  });
  stack.reset(base);
  for (let i = 0; i < 10; i += 1) {
    const next = core.cloneModel(stack.current());
    next.nodes.push({ id: `n${i}`, type: "agent", agentCode: "p" });
    stack.push(next);
  }
  assert.equal(stack.size(), 3);
});
