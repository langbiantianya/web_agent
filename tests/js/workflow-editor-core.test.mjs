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
  // 逐字段精确断言：任何字段都不得为 null / undefined。
  const topFields = ["entry", "inputTemplate", "nodes", "edges", "layout"];
  topFields.forEach((key) => {
    assert.notEqual(back[key], null, `顶层字段 ${key} 不应为 null`);
    assert.notEqual(back[key], undefined, `顶层字段 ${key} 不应为 undefined`);
  });
  const nodeFields = [
    "id",
    "type",
    "label",
    "agentCode",
    "outputKey",
    "prompt",
    "requiredApprovals",
    "approvalTimeoutSec",
    "approvalTimeoutAction",
  ];
  back.nodes.forEach((node) => {
    nodeFields.forEach((key) => {
      assert.notEqual(node[key], null, `节点字段 ${key} 不应为 null`);
      assert.notEqual(node[key], undefined, `节点字段 ${key} 不应为 undefined`);
    });
  });
  back.edges.forEach((edge) => {
    ["from", "to", "when"].forEach((key) => {
      assert.notEqual(edge[key], null, `边字段 ${key} 不应为 null`);
      assert.notEqual(edge[key], undefined, `边字段 ${key} 不应为 undefined`);
    });
  });
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

// --- 以下为修复轮 1/5 新增用例 ---

test("M1 toModel 对非数组 nodes/edges 不抛异常", () => {
  const cases = [
    { nodes: {}, edges: "x" },
    { nodes: "abc" },
    { nodes: 42 },
    { nodes: null },
    { nodes: [], edges: { a: 1 } },
  ];
  cases.forEach((input) => {
    const model = core.toModel(input);
    assert.deepEqual(model.nodes, []);
    assert.deepEqual(model.edges, []);
  });
});

test("M1 toModel 收到 null/undefined 也不抛异常", () => {
  assert.deepEqual(core.toModel(null).nodes, []);
  assert.deepEqual(core.toModel(undefined).nodes, []);
  assert.deepEqual(core.toModel({}).edges, []);
});

test("M2 computeLayout 遇到 __proto__ 节点 id 不崩且不丢节点", () => {
  const model = core.toModel({
    entry: "__proto__",
    nodes: [
      { id: "__proto__", type: "agent", agentCode: "p" },
      { id: "normal", type: "agent", agentCode: "p" },
    ],
    edges: [{ from: "__proto__", to: "normal" }],
  });
  const layout = core.computeLayout(model);
  assert.ok(Object.keys(layout.nodes).includes("__proto__"));
  assert.ok(Object.keys(layout.nodes).includes("normal"));
  assert.ok(layout.nodes.normal.x > layout.nodes.__proto__.x);
});

test("M2 toModel 保留 __proto__ 节点的布局坐标", () => {
  // 用 JSON.parse 构造，模拟服务端回读到的普通 JSON 键。
  const definition = JSON.parse(
    '{"nodes":[{"id":"__proto__","type":"agent"}],' +
    '"edges":[],"layout":{"nodes":{"__proto__":{"x":10,"y":20}}}}',
  );
  const model = core.toModel(definition);
  assert.ok(Object.keys(model.layout.nodes).includes("__proto__"));
  assert.deepEqual(model.layout.nodes.__proto__, { x: 10, y: 20 });
});

test("M2 其余特殊 id 同样不崩不丢", () => {
  const special = ["constructor", "prototype", "toString", "hasOwnProperty"];
  const nodes = special.map((id) => ({ id, type: "agent", agentCode: "p" }));
  const model = core.toModel({ nodes, edges: [] });
  const layout = core.computeLayout(model);
  assert.deepEqual(Object.keys(layout.nodes).sort(), special.slice().sort());
});

test("M2 clone 之后 __proto__ 键仍是 own property", () => {
  const definition = JSON.parse(
    '{"nodes":[{"id":"__proto__","type":"agent"}],"edges":[],' +
    '"layout":{"nodes":{"__proto__":{"x":10,"y":20}}}}',
  );
  const model = core.toModel(definition);
  const copied = core.cloneModel(model);
  assert.ok(Object.keys(copied.layout.nodes).includes("__proto__"));
  assert.equal(
    Object.prototype.hasOwnProperty.call(copied.layout.nodes, "__proto__"),
    true,
  );
  assert.deepEqual(copied.layout.nodes.__proto__, { x: 10, y: 20 });
});

test("L1 toDefinition 对缺 layout 的裸 model 不抛异常", () => {
  const back = core.toDefinition({
    entry: "a",
    inputTemplate: "",
    nodes: [],
    edges: [],
  });
  assert.deepEqual(back.layout.nodes, {});
  assert.deepEqual(back.layout.viewport, { x: 0, y: 0, scale: 1 });
});

test("L5 connectNodes 的三个 reason 契约", () => {
  const model = core.toModel({
    nodes: [
      { id: "a", type: "agent", agentCode: "p" },
      { id: "b", type: "agent", agentCode: "p" },
    ],
    edges: [{ from: "a", to: "b" }],
  });
  assert.equal(core.connectNodes(model, "a", "a").reason, "self_loop");
  assert.equal(core.connectNodes(model, "a", "b").reason, "duplicate");
  assert.equal(core.connectNodes(model, "a", "missing").reason, "unknown_node");
  assert.equal(core.connectNodes(model, "missing", "a").reason, "unknown_node");
  assert.equal(core.connectNodes(model, "b", "a").reason, undefined);
});

test("L5 computeLayout 用最长路径分层而非 BFS 分层", () => {
  // a→b→c→d 且 a→d：BFS 会把 d 放第 1 层，最长路径应放第 3 层。
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
      { from: "b", to: "c" },
      { from: "c", to: "d" },
      { from: "a", to: "d" },
    ],
  });
  const layout = core.computeLayout(model);
  const { a, b, c, d } = layout.nodes;
  // 层轴是 x，层号越大 x 越大。
  assert.ok(a.x < b.x);
  assert.ok(b.x < c.x);
  assert.ok(c.x < d.x);
  // BFS 会让 d 与 b 同层（x 相同），最长路径必须严格更大。
  assert.notEqual(d.x, b.x);
  // 相邻层步长一致，且 d 距 a 恰好 3 层。
  assert.equal(b.x - a.x, c.x - b.x);
  assert.equal(c.x - b.x, d.x - c.x);
  assert.equal(d.x - a.x, 3 * (b.x - a.x));
});

test("L5 computeLayout 不丢孤立节点", () => {
  const model = core.toModel({
    entry: "a",
    nodes: [
      { id: "a", type: "agent", agentCode: "p" },
      { id: "b", type: "agent", agentCode: "p" },
      { id: "lonely", type: "agent", agentCode: "p" },
    ],
    edges: [{ from: "a", to: "b" }],
  });
  const layout = core.computeLayout(model);
  assert.deepEqual(
    Object.keys(layout.nodes).sort(),
    ["a", "b", "lonely"],
  );
});

test("L5 撤销栈丢弃的是最旧的记录", () => {
  const stack = core.createUndoStack(3);
  const base = core.toModel({ nodes: [], edges: [] });
  stack.reset(base);
  // 依次压入 4 个状态：第 i 个状态含节点 n1..ni。
  for (let i = 1; i <= 4; i += 1) {
    const next = core.cloneModel(stack.current());
    next.nodes.push({ id: `n${i}`, type: "agent", agentCode: "p" });
    stack.push(next);
  }
  assert.equal(stack.size(), 3);
  const ids = (state) => state.nodes.map((node) => node.id).join(",");
  // undo 依次回到第 4、3、2 次 push 之前的状态。
  assert.equal(ids(stack.undo()), "n1,n2,n3");
  assert.equal(ids(stack.undo()), "n1,n2");
  assert.equal(ids(stack.undo()), "n1");
  // 第 4 次不再继续回退：reset 时的空状态被当作最旧记录丢弃了。
  assert.equal(stack.canUndo(), false);
  assert.equal(ids(stack.undo()), "n1");
});
