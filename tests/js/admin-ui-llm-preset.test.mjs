/**
 * LLM「服务商预设」下拉的自动填充守卫。
 *
 * 页面上有**两张** action 都是 ``/admin/llm`` 的表单：上面的「搜索 /
 * 排序」过滤表单（GET）和下面的「创建端点」表单（POST）。预设下拉要
 * 填的是后者。
 *
 * 之前的实现用 ``document.querySelector('form[action="/admin/llm"]')``
 * 取表单——命中的是排在文档前面的过滤表单，于是预设把值写进了一个
 * 根本没有这些字段的表单，用户看到的就是「选了预设没反应」。
 *
 * 这里用最小的 DOM 桩把 ``bindLlmPresetPicker`` 跑一遍：两张表单都摆
 * 上，再触发一次 change，断言**创建表单**拿到了预设的编码 / 名称 / 地址。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, "..", "..", "services", "admin_web", "static", "admin-ui.js"),
  "utf8",
);

/** 从源码里切出某个具名函数（按大括号配平）。 */
function extractFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  let depth = 0;
  let index = source.indexOf("{", start);
  for (; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    else if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  return source.slice(start, index + 1);
}

function makeField(value = "") {
  return {
    value,
    focused: false,
    focus() {
      this.focused = true;
    },
  };
}

function makeForm(fields) {
  return {
    elements: {
      namedItem(name) {
        return fields[name] || null;
      },
    },
  };
}

function loadBinder(document) {
  const factory = new Function(
    "document",
    `${extractFunction("bindLlmPresetPicker")}; return bindLlmPresetPicker;`,
  );
  return factory(document);
}

test("预设下拉把编码 / 名称 / 地址填进创建表单，而不是过滤表单", () => {
  const filterForm = makeForm({ q: makeField(""), sort: makeField("") });
  const createForm = makeForm({
    code: makeField(""),
    name: makeField(""),
    base_url: makeField(""),
    api_key: makeField(""),
  });

  const option = {
    dataset: {
      code: "deepseek",
      name: "DeepSeek",
      baseUrl: "https://api.deepseek.com/v1",
    },
  };
  const handlers = [];
  const select = {
    selectedOptions: [option],
    addEventListener(type, handler) {
      if (type === "change") handlers.push(handler);
    },
  };

  const document = {
    querySelectorAll(selector) {
      if (selector === "select[data-llm-preset]") return [select];
      if (selector === 'form[action="/admin/llm"]') {
        // 过滤表单排在前面——这正是回归的成因。
        return [filterForm, createForm];
      }
      return [];
    },
  };

  const bindLlmPresetPicker = loadBinder(document);
  bindLlmPresetPicker();

  assert.equal(handlers.length, 1, "预设下拉没有绑定 change 事件");
  handlers[0]();

  assert.equal(createForm.elements.namedItem("code").value, "deepseek");
  assert.equal(createForm.elements.namedItem("name").value, "DeepSeek");
  assert.equal(
    createForm.elements.namedItem("base_url").value,
    "https://api.deepseek.com/v1",
  );
  assert.equal(filterForm.elements.namedItem("q").value, "", "过滤表单被写脏了");
});

test("选「自定义端点」时不动用户已经填好的内容", () => {
  const createForm = makeForm({
    code: makeField("my-code"),
    name: makeField("My Name"),
    base_url: makeField("https://my.example/v1"),
    api_key: makeField(""),
  });
  const option = { dataset: {} };
  const handlers = [];
  const select = {
    selectedOptions: [option],
    addEventListener(type, handler) {
      if (type === "change") handlers.push(handler);
    },
  };
  const document = {
    querySelectorAll(selector) {
      if (selector === "select[data-llm-preset]") return [select];
      if (selector === 'form[action="/admin/llm"]') return [createForm];
      return [];
    },
  };
  const bindLlmPresetPicker = loadBinder(document);
  bindLlmPresetPicker();
  handlers[0]();
  assert.equal(createForm.elements.namedItem("code").value, "my-code");
  assert.equal(
    createForm.elements.namedItem("base_url").value,
    "https://my.example/v1",
  );
});
