/**
 * 极简 DOM 桩：只实现 workflow-editor.js 真正用到的那几个 API。
 *
 * 目的是在**不装任何前端依赖**的前提下，给画布渲染层一套可自动跑的
 * 验收用例。手写 DOM 桩比引 jsdom 更省事，也更符合本项目「不引前端依赖」的约束。
 */

/** class 名集合：读写元素上的 class 属性。 */
class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  names() {
    return (this.element.getAttribute("class") || "")
      .split(/\s+/)
      .filter((name) => name);
  }

  add(name) {
    const names = this.names();
    if (!names.includes(name)) {
      names.push(name);
      this.element.setAttribute("class", names.join(" "));
    }
  }

  remove(name) {
    this.element.setAttribute(
      "class",
      this.names().filter((item) => item !== name).join(" "),
    );
  }

  contains(name) {
    return this.names().includes(name);
  }

  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : Boolean(force);
    if (on) {
      this.add(name);
    } else {
      this.remove(name);
    }
  }
}

/** 极简元素：属性、类名、树、监听器、矩形。 */
class FakeElement {
  constructor(tagName, namespaceURI = null) {
    this.tagName = tagName;
    this.namespaceURI = namespaceURI;
    this.attributes = new Map();
    this.children = [];
    this.parentNode = null;
    this.textContent = "";
    this.value = "";
    this.listeners = new Map();
    this.classList = new FakeClassList(this);
    this.rect = { left: 0, top: 0, width: 800, height: 600 };
    // 普通属性：表单控件用。
    this.type = "";
    this.rows = 0;
    this.disabled = false;
    this.selected = false;
    // 光标位置：null 表示「跟随 value 的长度」。
    this._selectionStart = null;
    this._selectionEnd = null;
  }

  /** className 与 class 属性保持同步。 */
  get className() {
    return this.getAttribute("class") || "";
  }

  set className(value) {
    this.setAttribute("class", String(value));
  }

  /** hidden 与 hidden 属性保持同步（真实浏览器就是这样）。 */
  get hidden() {
    return this.hasAttribute("hidden");
  }

  set hidden(value) {
    if (value) {
      this.setAttribute("hidden", "hidden");
    } else {
      this.removeAttribute("hidden");
    }
  }

  /** 光标位置：没显式设置过时取文本长度。 */
  get selectionStart() {
    if (this._selectionStart === null) {
      return String(this.value).length;
    }
    return this._selectionStart;
  }

  set selectionStart(value) {
    this._selectionStart = Number(value);
  }

  get selectionEnd() {
    if (this._selectionEnd === null) {
      return this.selectionStart;
    }
    return this._selectionEnd;
  }

  set selectionEnd(value) {
    this._selectionEnd = Number(value);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  focus() {}

  blur() {}

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  set innerHTML(value) {
    if (value === "") {
      this.children.forEach((child) => {
        child.parentNode = null;
      });
      this.children = [];
    }
  }

  get innerHTML() {
    return "";
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type).push(handler);
  }

  /** 手工触发一个事件；target 可以指定成别的元素。 */
  dispatch(type, init = {}) {
    const event = {
      type,
      target: init.target || this,
      button: init.button === undefined ? 0 : init.button,
      clientX: init.clientX === undefined ? 0 : init.clientX,
      clientY: init.clientY === undefined ? 0 : init.clientY,
      pointerId: init.pointerId === undefined ? 1 : init.pointerId,
      deltaY: init.deltaY === undefined ? 0 : init.deltaY,
      code: init.code || "",
      key: init.key || "",
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      stopPropagation() {},
      stopImmediatePropagation() {},
    };
    (this.listeners.get(type) || []).forEach((handler) => handler(event));
    return event;
  }

  /** 只支持 ".class" 与 "tag" 两种选择器。 */
  matches(selector) {
    if (selector.startsWith(".")) {
      return this.classList.contains(selector.slice(1));
    }
    return this.tagName === selector;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches(selector)) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }

  /** 深度优先遍历，供测试查找节点。 */
  walk(visit) {
    this.children.forEach((child) => {
      visit(child);
      child.walk(visit);
    });
  }

  getBoundingClientRect() {
    return this.rect;
  }

  setPointerCapture() {}

  releasePointerCapture() {}

  hasPointerCapture() {
    return false;
  }
}

/**
 * 造一套最小页面：画布容器 + 调色板 + 检查器 + 提示条 +
 * 初始状态 textarea + 隐藏 definition input。
 */
export function createDom({ state, definition = "", lang = "zh-CN" } = {}) {
  const registry = new Map();
  const documentListeners = new Map();
  const root = new FakeElement("div");

  const document = {
    readyState: "complete",
    documentElement: { lang },
    createElementNS(namespaceURI, tagName) {
      return new FakeElement(tagName, namespaceURI);
    },
    createElement(tagName) {
      return new FakeElement(tagName);
    },
    getElementById(id) {
      return registry.get(id) || null;
    },
    /** 只支持 ".class" 选择器，返回数组。 */
    querySelectorAll(selector) {
      const hits = [];
      root.walk((node) => {
        if (node.matches && node.matches(selector)) {
          hits.push(node);
        }
      });
      return hits;
    },
    /** 返回第一个匹配（取不到就是 null）。另支持 [name="x"] 与 tag[name="x"] 属性选择器。 */
    querySelector(selector) {
      const attr = /^([a-zA-Z]+)?\[([\w-]+)="([^"]*)"\]$/.exec(
        String(selector || ""),
      );
      if (attr) {
        const wantTag = attr[1] ? attr[1].toLowerCase() : null;
        const wantName = attr[2];
        const wantValue = attr[3];
        let found = null;
        root.walk((node) => {
          if (found) {
            return;
          }
          const tagOk =
            !wantTag ||
            String(node.tagName || "").toLowerCase() === wantTag;
          const attrOk =
            node.getAttribute && node.getAttribute(wantName) === wantValue;
          if (tagOk && attrOk) {
            found = node;
          }
        });
        return found;
      }
      const hits = this.querySelectorAll(selector);
      return hits.length ? hits[0] : null;
    },
    addEventListener(type, handler) {
      if (!documentListeners.has(type)) {
        documentListeners.set(type, []);
      }
      documentListeners.get(type).push(handler);
    },
    removeEventListener() {},
    /** 手工触发文档级事件（键盘用）。 */
    dispatch(type, init = {}) {
      const event = {
        type,
        target: init.target || document,
        code: init.code || "",
        key: init.key || "",
        ctrlKey: Boolean(init.ctrlKey),
        metaKey: Boolean(init.metaKey),
        shiftKey: Boolean(init.shiftKey),
        altKey: Boolean(init.altKey),
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
      };
      (documentListeners.get(type) || []).forEach((handler) => handler(event));
      return event;
    },
  };

  const canvas = new FakeElement("div");
  canvas.setAttribute("id", "workflow-editor-canvas");
  canvas.setAttribute("class", "workflow-editor-canvas");
  registry.set("workflow-editor-canvas", canvas);
  root.appendChild(canvas);

  const paletteAgent = new FakeElement("button");
  paletteAgent.setAttribute("class", "btn btn-secondary workflow-palette-item");
  paletteAgent.setAttribute("data-node-type", "agent");
  root.appendChild(paletteAgent);

  const paletteApproval = new FakeElement("button");
  paletteApproval.setAttribute(
    "class",
    "btn btn-secondary workflow-palette-item",
  );
  paletteApproval.setAttribute("data-node-type", "approval");
  root.appendChild(paletteApproval);

  const inspector = new FakeElement("div");
  inspector.setAttribute("id", "workflow-editor-inspector");
  inspector.setAttribute("class", "workflow-editor-inspector");
  registry.set("workflow-editor-inspector", inspector);
  root.appendChild(inspector);

  const errorList = new FakeElement("ul");
  errorList.setAttribute("id", "workflow-editor-errors");
  errorList.setAttribute("class", "workflow-editor-errors");
  const errorBar = new FakeElement("div");
  errorBar.setAttribute("id", "workflow-editor-errorbar");
  errorBar.setAttribute("class", "workflow-editor-errorbar");
  errorBar.setAttribute("hidden", "hidden");
  errorBar.appendChild(errorList);
  registry.set("workflow-editor-errorbar", errorBar);
  registry.set("workflow-editor-errors", errorList);
  root.appendChild(errorBar);

  const stateBox = new FakeElement("textarea");
  stateBox.setAttribute("id", "workflow-editor-state");
  stateBox.value = JSON.stringify(state || {});
  registry.set("workflow-editor-state", stateBox);
  root.appendChild(stateBox);

  const definitionBox = new FakeElement("input");
  definitionBox.setAttribute("id", "workflow-editor-definition");
  definitionBox.value = definition;
  registry.set("workflow-editor-definition", definitionBox);
  root.appendChild(definitionBox);

  // 保存表单里的 CSRF 隐藏字段（校验端点要用）。
  const csrfBox = new FakeElement("input");
  csrfBox.setAttribute("type", "hidden");
  csrfBox.setAttribute("name", "csrf_token");
  csrfBox.value = "csrf-demo";
  root.appendChild(csrfBox);

  // 工具栏按钮。只读时由页面渲染成 disabled，这里交给用例按需设置。
  const toolbar = {};
  ["undo", "redo", "layout", "fit", "json-toggle"].forEach((name) => {
    const button = new FakeElement("button");
    button.setAttribute("id", `workflow-editor-${name}`);
    registry.set(`workflow-editor-${name}`, button);
    root.appendChild(button);
    toolbar[name] = button;
  });

  // JSON 预览面板：默认隐藏，里面有一个 pre。
  const jsonView = new FakeElement("pre");
  jsonView.setAttribute("id", "workflow-editor-json");
  const jsonPanel = new FakeElement("div");
  jsonPanel.setAttribute("id", "workflow-editor-json-panel");
  jsonPanel.setAttribute("hidden", "hidden");
  jsonPanel.appendChild(jsonView);
  root.appendChild(jsonPanel);
  registry.set("workflow-editor-json-panel", jsonPanel);
  registry.set("workflow-editor-json", jsonView);

  const window = {};
  return {
    document,
    window,
    canvas,
    stateBox,
    definitionBox,
    csrfBox,
    inspector,
    errorBar,
    errorList,
    toolbar,
    jsonPanel,
    jsonView,
    registry,
    root,
  };
}
