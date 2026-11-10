/**
 * 管理端中英文词表的结构守卫。
 *
 * ``admin-ui.js`` 里的 ``ZH``（逐条文案）与 ``ZH_PATTERNS``（动态文案模式）
 * 是**运行时**才被消费的：一处写错（正则写成字符串、替换串缺位），
 * 中文界面就会整页停在英文，而且只在浏览器里才暴露。
 *
 * 这里把两个词表抽出来做结构校验：
 *
 * - ``ZH`` 的每个取值必须是非空字符串；
 * - ``ZH_PATTERNS`` 的每一项必须是 ``[RegExp, string]``——第一项不是
 *   正则就会让 ``pattern.test`` 抛错，整段翻译直接失效。
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

function extractObject(name) {
  const start = source.indexOf(`const ${name} = {`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  const end = source.indexOf("\n  };", start);
  assert.notEqual(end, -1, `${name} 没有正常结束`);
  // 词表里只有字面量，没有变量引用，可以安全求值。
  return eval(`(${source.slice(start + `const ${name} = `.length, end + 4)})`);
}

function extractArray(name) {
  const start = source.indexOf(`const ${name} = [`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  const end = source.indexOf("\n  ];", start);
  assert.notEqual(end, -1, `${name} 没有正常结束`);
  // 词表里只有字面量，没有变量引用，可以安全求值。
  return eval(source.slice(start + `const ${name} = `.length, end + 4));
}

const ZH = extractObject("ZH");
const ZH_PATTERNS = extractArray("ZH_PATTERNS");

test("ZH 词表的每个取值都是非空字符串", () => {
  for (const [key, value] of Object.entries(ZH)) {
    assert.equal(typeof value, "string", `${key} 的译文不是字符串`);
    assert.ok(value.length > 0, `${key} 的译文是空串`);
  }
});

test("ZH_PATTERNS 的每一项都是 [正则, 替换串]", () => {
  for (const [index, entry] of ZH_PATTERNS.entries()) {
    assert.ok(Array.isArray(entry), `第 ${index} 项不是数组`);
    assert.equal(entry.length, 2, `第 ${index} 项不是二元组`);
    assert.equal(
      typeof entry[0],
      "object",
      `第 ${index} 项的第一个元素不是正则对象`,
    );
    assert.equal(
      typeof entry[0].test,
      "function",
      `第 ${index} 项的第一个元素不是正则（会让 pattern.test 抛错）`,
    );
    assert.equal(typeof entry[1], "string", `第 ${index} 项的替换串不是字符串`);
  }
});

test("删除确认对话框的文案能翻成中文", () => {
  const translate = (text) => {
    if (Object.hasOwn(ZH, text)) return ZH[text];
    for (const [pattern, replacement] of ZH_PATTERNS) {
      if (pattern.test(text)) return text.replace(pattern, replacement);
    }
    return text;
  };
  assert.equal(
    translate("Delete Agent ux-verify-agent"),
    "删除智能体 ux-verify-agent",
  );
  assert.equal(
    translate("Archive Skill ux-verify-scratch"),
    "归档技能 ux-verify-scratch",
  );
  assert.equal(translate("Undo delete"), "撤销删除");
  assert.equal(translate("Apply"), "应用");
  // 编辑面板标题与撤销按钮的可访问名称也要能翻。
  assert.equal(translate("Edit endpoint minimax"), "编辑端点 minimax");
  assert.equal(translate("Edit agent ux-verify-agent"), "编辑智能体 ux-verify-agent");
  assert.equal(
    translate("Undo delete LLM endpoint"),
    "撤销删除 LLM endpoint",
  );
});
