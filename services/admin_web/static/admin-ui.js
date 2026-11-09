(() => {
  "use strict";

  const LOCALE_KEY = "web_agent.admin.locale";
  const APPEARANCE_KEY = "web_agent.admin.appearance.v1";
  const SUPPORTED_LOCALES = new Set(["en", "zh-CN"]);
  const APPEARANCE_DEFAULTS = {
    theme: "fluent",
    colorMode: "system",
    density: "comfortable",
  };
  const ALLOWED_APPEARANCE = {
    theme: new Set(["fluent", "cloudscape", "material"]),
    colorMode: new Set(["light", "dark", "system"]),
    density: new Set(["comfortable", "compact"]),
  };
  const SKIP_TAGS = new Set([
    "SCRIPT",
    "STYLE",
    "PRE",
    "CODE",
    "TEXTAREA",
    "INPUT",
    "SELECT",
  ]);

  const ZH = {
    "API key": "API 密钥",
    "Access control": "访问控制",
    Accounts: "账户",
    Actions: "操作",
    "Active OAuth admins": "活跃 OAuth 管理员",
    "Add mapping": "添加映射",
    Admin: "管理员",
    "Admin login": "管理员登录",
    "Admin navigation": "管理后台导航",
    "Admin visual theme": "管理后台主题",
    Administration: "管理后台",
    "Administrator identity": "管理员身份",
    Agent: "智能体",
    Agents: "智能体",
    "Alibaba Bailian (DashScope)": "阿里百炼（DashScope）",
    Any: "任意",
    Appearance: "外观",
    "Apply filters": "应用筛选",
    Archive: "归档",
    archived: "已归档",
    "Authorization endpoint": "授权端点",
    "Auto create client users": "自动创建客户端用户",
    "AWS Cloudscape": "AWS Cloudscape",
    "Base URL": "基础 URL",
    "Bind known pages to RBAC object/action permissions. UI bindings never replace API authorization.":
      "将已知页面绑定到 RBAC 对象/操作权限。界面绑定不会替代 API 授权。",
    Builtin: "内置",
    builtin: "内置",
    "Casbin policy": "Casbin 策略",
    "Casbin role": "Casbin 角色",
    "Casbin roles": "Casbin 角色",
    Claim: "声明",
    "Clear existing client secret": "清除现有客户端密钥",
    Client: "客户端",
    client: "客户端",
    "Client ID": "客户端 ID",
    "Client login": "客户端登录",
    "Client secret": "客户端密钥",
    "Client secret basic": "客户端密钥 Basic",
    "Client secret post": "客户端密钥 Post",
    "Cloud Control Plane": "云控制平面",
    "Choose a cloud-console visual language. Preferences are stored server-side for this administrator.":
      "选择云控制台视觉风格。主题偏好会保存在当前管理员的账户中。",
    "Choosing a preset fills the endpoint code, name and base URL; it never fills the API key or the model.":
      "选择预设会自动填入端点编码、名称和基础 URL；API 密钥与模型不会被填。",
    Code: "代码",
    "Color mode": "颜色模式",
    Comfortable: "舒适",
    comfortable: "舒适",
    Command: "命令",
    command: "命令",
    Compact: "紧凑",
    compact: "紧凑",
    Component: "组件",
    "Component permissions": "组件权限",
    "Components without bindings are hidden. Hidden components are not an API authorization boundary.":
      "未绑定权限的组件会被隐藏。隐藏组件并不构成 API 授权边界。",
    "Configured resources": "已配置资源",
    "Connection credentials stay encrypted and are never rendered back into the console.":
      "连接凭据始终加密保存，且不会回显到控制台。",
    Conversations: "会话",
    "Create agent": "创建智能体",
    "Create and send password": "创建并发送密码",
    "Create endpoint": "创建端点",
    "Create provider": "创建身份提供方",
    "Create resource": "创建资源",
    "Create skill": "创建技能",
    "Create workflow": "创建工作流",
    Created: "已创建",
    "Current mappings": "当前映射",
    "Custom endpoint (fill in manually)": "自定义端点（手动填写）",
    Dark: "深色",
    dark: "深色",
    "Definition JSON": "定义 JSON",
    Delete: "删除",
    "Dense operations layout, flat panels, and a dark command bar.":
      "高密度运维布局、扁平面板和深色命令栏。",
    Density: "密度",
    Description: "描述",
    Disable: "禁用",
    Disabled: "已禁用",
    disabled: "已禁用",
    "Discovery URL": "发现 URL",
    "Display name": "显示名称",
    Draft: "草稿",
    draft: "草稿",
    Duration: "持续时间",
    "Edit component permissions": "编辑组件权限",
    "Edit configuration": "编辑配置",
    "Edit connection": "编辑连接",
    "Edit definition": "编辑定义",
    "Edit endpoint": "编辑端点",
    "Edit page permissions": "编辑页面权限",
    "Edit provider": "编辑身份提供方",
    "Edit skill": "编辑技能",
    Email: "邮箱",
    Enable: "启用",
    Enabled: "已启用",
    enabled: "已启用",
    Endpoint: "端点",
    "Endpoints are called with the OpenAI-compatible protocol (POST /v1/chat/completions), so DeepSeek, Alibaba Bailian, Qwen, vLLM and self-hosted services all work. Lower priority numbers are attempted first. API keys remain write-only.":
      "端点统一走 OpenAI 兼容协议（POST /v1/chat/completions），可接入 DeepSeek、阿里百炼、Qwen、vLLM 或自建兼容服务。优先级数字越小越先尝试。API 密钥保持只写。",
    "External role": "外部角色",
    "External value": "外部值",
    Failed: "失败",
    "Fetch models": "获取模型列表",
    "Fluent Design": "Fluent 设计",
    "Back to sign in": "返回登录",
    "Back to OAuth": "返回 OAuth 登录",
    "Enter your username and we will send a new password to the registered contact channel.":
      "输入用户名，我们会向已登记的联系方式发送新密码。",
    "Forgot password": "忘记密码",
    "Hide username and password": "隐藏账号密码登录",
    "Reset password": "重置密码",
    "Send new password": "发送新密码",
    "Use username and password": "使用账号密码登录",
    "Google Material": "Google Material",
    Group: "分组",
    HTTP: "HTTP",
    "HTTP URL": "HTTP URL",
    Injection: "注入方式",
    Issuer: "签发方",
    "JWKS URI": "JWKS URI",
    Language: "语言",
    "Layered surfaces, acrylic accents, and Segoe-inspired controls.":
      "分层表面、亚克力强调色和 Segoe 风格控件。",
    "Leave blank to keep current key": "留空以保留当前密钥",
    "Leave blank to keep current secret": "留空以保留当前密钥",
    Light: "浅色",
    light: "浅色",
    "LLM endpoints": "LLM 端点",
    "Login scopes": "登录 Scope",
    "Manage graph definitions directly against the shared workflow repository.":
      "直接针对共享工作流仓库管理图定义。",
    "Manage platform configuration, access policy, and operations.":
      "管理平台配置、访问策略和运维操作。",
    MCP: "MCP",
    "MCP servers": "MCP 服务器",
    Menu: "菜单",
    Model: "模型",
    Name: "名称",
    "No OAuth provider is configured.": "尚未配置 OAuth 身份提供方。",
    "No admin login.": "无管理员登录。",
    "No admin login": "无管理员登录",
    "No audit records found.": "未找到审计记录。",
    "No client secret": "没有客户端密钥",
    "No dashboard components available": "没有可用的仪表盘组件",
    "No resources configured.": "尚未配置资源。",
    "OAuth administrator": "OAuth 管理员",
    "OAuth administrators are authorized through Casbin. Platform super admins bypass role checks.":
      "OAuth 管理员通过 Casbin 授权。平台超级管理员跳过角色检查。",
    "OAuth providers": "OAuth 身份提供方",
    "OAuth role mappings": "OAuth 角色映射",
    "Optional for public clients": "公共客户端可选",
    Outcome: "结果",
    Overview: "概览",
    Operations: "运维",
    Page: "页面",
    "Page permissions": "页面权限",
    Password: "密码",
    "Password expires": "密码到期时间",
    Permissions: "权限",
    Phone: "手机号码",
    "Platform admins": "平台管理员",
    "Platform overview": "平台概览",
    "Platform owner": "平台所有者",
    "Platform super admin": "平台超级管理员",
    "Platform super admins": "平台超级管理员",
    Preferences: "偏好设置",
    Priority: "优先级",
    "Prompt, model, tool, and skill bindings are managed inside the isolated admin service.":
      "提示词、模型、工具和技能绑定由独立管理服务维护。",
    Provider: "身份提供方",
    "Provider code": "身份提供方代码",
    "Provider enabled": "启用身份提供方",
    "Provider preset": "服务商预设",
    Published: "已发布",
    published: "已发布",
    "Quick actions": "快捷操作",
    "Read only": "只读",
    "Recent invocations": "最近调用",
    "Refresh tools": "刷新工具",
    "Register server": "注册服务器",
    "Replacement API key": "替换 API 密钥",
    "Reserved for the built-in emergency and platform owner accounts.":
      "保留给内置应急账户和平台所有者账户。",
    Resources: "资源",
    "Return to sign in": "返回登录",
    "Review Casbin roles": "查看 Casbin 角色",
    "Review publication state and edit instruction packages without crossing the legacy client API.":
      "检查发布状态并编辑指令包，无需经过旧客户端 API。",
    "redacted tool invocation records are visible to this tenant.":
      "当前租户可以查看脱敏后的工具调用记录。",
    Role: "角色",
    "Role claim": "角色声明",
    "Role claim required": "必须提供角色声明",
    "Role sync": "角色同步",
    "Role sync max age (seconds)": "角色同步最大时效（秒）",
    Run: "运行",
    "Run ID": "运行 ID",
    Runs: "运行",
    "Resource created.": "资源已创建。",
    "Resource deleted.": "资源已删除。",
    "Resource state updated.": "资源状态已更新。",
    "Changes saved.": "更改已保存。",
    "MCP tool catalog refreshed.": "MCP 工具目录已刷新。",
    "The submitted values are invalid.": "提交的值无效。",
    "A resource with that code already exists.": "该代码对应的资源已存在。",
    "The requested resource no longer exists.": "请求的资源已不存在。",
    "The operation is not permitted.": "不允许执行此操作。",
    "The operation could not be completed.": "无法完成操作。",
    "The requested management page is unavailable.": "请求的管理页面不可用。",
    "CSRF validation failed.": "CSRF 校验失败。",
    "Critical system pages cannot be made inaccessible.": "关键系统页面不能被设为不可访问。",
    "You cannot bind permissions outside your own scope.": "不能绑定超出自身范围的权限。",
    "The requested tenant scope is invalid.": "请求的租户范围无效。",
    "You cannot change another tenant's UI policy.": "不能修改其他租户的界面策略。",
    "If the account exists, a new password was sent":
      "如果账户存在，新密码已发送。",
    invalid_origin: "请求来源无效。",
    provider_not_found: "未找到身份提供方。",
    state_missing: "登录状态缺失，请重试。",
    state_invalid: "登录状态无效，请重试。",
    login_csrf: "登录安全校验失败，请重试。",
    state_expired: "登录请求已过期，请重试。",
    state_replayed: "登录请求已使用，请重新登录。",
    provider_error: "身份提供方返回错误。",
    code_missing: "身份提供方未返回授权码。",
    admin_role_required: "当前账户没有管理后台角色。",
    password_resent: "新密码已发送。",
    Save: "保存",
    "Save changes": "保存更改",
    "Save connection": "保存连接",
    "Save endpoint": "保存端点",
    "Save preferences": "保存偏好设置",
    "Save provider": "保存身份提供方",
    "Save skill": "保存技能",
    Scopes: "Scope",
    "Secret configured": "已配置密钥",
    Security: "安全",
    Server: "服务器",
    Settings: "设置",
    "Sign in": "登录",
    "Sign out": "退出登录",
    Skill: "技能",
    "Skill body": "技能正文",
    Skills: "技能",
    "Skills (comma separated)": "技能（逗号分隔）",
    "Soft elevation, expressive shapes, and Google blue actions.":
      "柔和层级、表现力形状和 Google 蓝色操作。",
    Source: "来源",
    State: "状态",
    Status: "状态",
    Success: "成功",
    System: "跟随系统",
    system: "跟随系统",
    "System prompt": "系统提示词",
    "stdio command": "stdio 命令",
    Theme: "主题",
    "Test connection": "测试连接",
    "Timeout seconds": "超时时间（秒）",
    "Token auth method": "令牌认证方式",
    "Token endpoint": "令牌端点",
    Tool: "工具",
    "Tool audit": "工具审计",
    "Tool name": "工具名称",
    "Tools (comma separated)": "工具（逗号分隔）",
    Transport: "传输方式",
    Type: "类型",
    URL: "URL",
    "Upsert scope": "写入 Scope",
    "Use your organization identity. Roles are synchronized from OAuth claims.":
      "使用组织身份登录，角色通过 OAuth 声明同步。",
    Username: "用户名",
    "Username claim": "用户名字段",
    Version: "版本",
    Workflow: "工作流",
    Workflows: "工作流",
    "Your administrator account is active, but all dashboard components are hidden by the current UI policy.":
      "管理员账户已启用，但当前界面策略隐藏了所有仪表盘组件。",
    Yes: "是",
    owner: "所有者",
    active: "启用",
  };

  const ZH_PATTERNS = [
    [/^Continue with (.+)$/, "使用 $1 继续"],
    [/^New scope permissions for (.+)$/, "为 $1 设置新的 Scope 权限"],
    [/^New tenant scope for (.+)$/, "为 $1 设置新的租户范围"],
    [/^Tenant scope for (.+)$/, "租户范围：$1"],
    [/^(.+) sync age$/, "$1 同步时效"],
  ];

  function safeParse(value) {
    try {
      return JSON.parse(value || "null");
    } catch {
      return null;
    }
  }

  function getStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function setStorage(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // Browsers may disable storage in private or policy-managed contexts.
    }
  }

  function normalizeAppearance(raw) {
    const candidate = raw && typeof raw === "object" ? raw : {};
    return {
      theme: ALLOWED_APPEARANCE.theme.has(candidate.theme)
        ? candidate.theme
        : APPEARANCE_DEFAULTS.theme,
      colorMode: ALLOWED_APPEARANCE.colorMode.has(candidate.colorMode)
        ? candidate.colorMode
        : APPEARANCE_DEFAULTS.colorMode,
      density: ALLOWED_APPEARANCE.density.has(candidate.density)
        ? candidate.density
        : APPEARANCE_DEFAULTS.density,
    };
  }

  function readAppearance() {
    const stored = safeParse(getStorage(APPEARANCE_KEY));
    return stored ? normalizeAppearance(stored) : null;
  }

  function applyAppearance(appearance) {
    if (!appearance) return;
    document.documentElement.dataset.theme = appearance.theme;
    document.documentElement.dataset.colorMode = appearance.colorMode;
    document.documentElement.dataset.density = appearance.density;
  }

  function syncAppearanceForm(appearance) {
    if (!appearance) return;
    const form = document.querySelector('form[action="/admin/preferences"]');
    if (!form) return;
    const theme = form.querySelector(`input[name="theme"][value="${appearance.theme}"]`);
    if (theme) theme.checked = true;
    const colorMode = form.querySelector('select[name="color_mode"]');
    if (colorMode) colorMode.value = appearance.colorMode;
    const density = form.querySelector('select[name="density"]');
    if (density) density.value = appearance.density;
  }

  function focusFirstControl(container) {
    const control = container.querySelector(
      'input:not([type="hidden"]):not([disabled]), button:not([disabled]), select, textarea, a[href]',
    );
    if (control) control.focus({ preventScroll: true });
  }

  function syncLoginDisclosure(button) {
    const panel = document.getElementById(button.dataset.adminLoginToggle);
    if (!panel) return;
    const visible = panel.classList.contains("is-active");
    button.setAttribute("aria-expanded", String(visible));
    for (const label of button.querySelectorAll("[data-admin-login-toggle-label]")) {
      label.hidden = label.dataset.adminLoginToggleLabel !== (visible ? "hide" : "show");
    }
  }

  function syncLoginView(view) {
    const visible = !view.hidden;
    for (const control of document.querySelectorAll(`[aria-controls="${view.id}"]`)) {
      control.setAttribute("aria-expanded", String(visible));
    }
  }

  function showLoginView(showView, hideView, moveFocus = true) {
    if (!showView || !hideView) return;
    hideView.hidden = true;
    syncLoginView(hideView);
    showView.hidden = false;
    syncLoginView(showView);
    if (moveFocus) focusFirstControl(showView);
  }

  function bindLoginControls() {
    const toggles = Array.from(document.querySelectorAll("[data-admin-login-toggle]"));
    const panel = toggles.length
      ? document.getElementById(toggles[0].dataset.adminLoginToggle)
      : null;
    const primaryCard = document.querySelector(".primary-login-card");
    const disclosure = document.getElementById("toggle-super-admin-login");
    const signInView = document.getElementById("super-admin-sign-in-view");
    const resetView = document.getElementById("super-admin-password-reset-view");
    let hideTimer = null;

    if (toggles.length && panel) {
      for (const toggle of toggles) syncLoginDisclosure(toggle);

      const syncToggles = () => {
        for (const toggle of toggles) syncLoginDisclosure(toggle);
      };

      const showPasswordLogin = () => {
        if (panel.classList.contains("is-active")) return;
        window.clearTimeout(hideTimer);
        hideTimer = null;
        panel.hidden = false;
        void panel.offsetWidth;
        panel.classList.add("is-active");
        primaryCard?.classList.add("is-replaced");
        syncToggles();
        focusFirstControl(panel);
      };

      const showOAuthLogin = () => {
        if (!panel.classList.contains("is-active")) return;
        panel.classList.remove("is-active");
        primaryCard?.classList.remove("is-replaced");
        if (signInView && resetView) {
          showLoginView(signInView, resetView, false);
        }
        syncToggles();
        disclosure?.focus();
        window.clearTimeout(hideTimer);
        hideTimer = window.setTimeout(() => {
          if (!panel.classList.contains("is-active")) panel.hidden = true;
          hideTimer = null;
        }, 260);
      };

      for (const toggle of toggles) {
        toggle.addEventListener("click", () => {
          if (panel.classList.contains("is-active")) {
            showOAuthLogin();
          } else {
            showPasswordLogin();
          }
        });
      }
    }

    for (const trigger of document.querySelectorAll("[data-admin-view-show]")) {
      const showView = document.getElementById(trigger.dataset.adminViewShow);
      const hideView = document.getElementById(trigger.dataset.adminViewHide);
      if (!showView || !hideView) continue;
      syncLoginView(showView);
      trigger.addEventListener("click", () => showLoginView(showView, hideView));
    }
  }

  function currentLocale() {
    const stored = getStorage(LOCALE_KEY);
    if (SUPPORTED_LOCALES.has(stored)) return stored;
    return navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
  }

  function translatedText(text, locale) {
    if (locale !== "zh-CN") return text;
    if (Object.hasOwn(ZH, text)) return ZH[text];
    for (const [pattern, replacement] of ZH_PATTERNS) {
      if (pattern.test(text)) return text.replace(pattern, replacement);
    }
    return text;
  }

  function shouldSkip(node) {
    const parent = node.parentElement;
    return !parent || SKIP_TAGS.has(parent.tagName);
  }

  function translateTextNodes(locale) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      if (!shouldSkip(walker.currentNode)) nodes.push(walker.currentNode);
    }
    for (const node of nodes) {
      const source = node.nodeValue || "";
      const key = source.trim();
      if (!key) continue;
      const translated = translatedText(key, locale);
      if (translated === key) continue;
      const leading = source.match(/^\s*/)?.[0] || "";
      const trailing = source.match(/\s*$/)?.[0] || "";
      node.nodeValue = `${leading}${translated}${trailing}`;
    }
  }

  function translateAttributes(locale) {
    for (const name of ["aria-label", "title", "placeholder"]) {
      for (const element of document.querySelectorAll(`[${name}]`)) {
        const source = element.getAttribute(name) || "";
        const translated = translatedText(source.trim(), locale);
        if (translated !== source.trim()) element.setAttribute(name, translated);
      }
    }
  }

  function translateDocument(locale) {
    document.documentElement.lang = locale;
    translateTextNodes(locale);
    translateAttributes(locale);
    if (locale === "zh-CN") {
      const title = document.title.replace(" · web_agent Admin", "");
      document.title = `${translatedText(title, locale)} · web_agent 管理后台`;
    }
    for (const button of document.querySelectorAll("[data-admin-locale]")) {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.adminLocale === locale),
      );
    }
  }

  function bindLocaleSwitch(locale) {
    for (const button of document.querySelectorAll("[data-admin-locale]")) {
      button.addEventListener("click", () => {
        const nextLocale = button.dataset.adminLocale;
        if (!SUPPORTED_LOCALES.has(nextLocale)) return;
        setStorage(LOCALE_KEY, nextLocale);
        if (nextLocale === locale) return;
        window.location.reload();
      });
    }
  }

  function bindAppearancePersistence(appearance) {
    const form = document.querySelector('form[action="/admin/preferences"]');
    if (!form) return;
    syncAppearanceForm(appearance);
    form.addEventListener("submit", () => {
      const data = new FormData(form);
      const next = normalizeAppearance({
        theme: data.get("theme"),
        colorMode: data.get("color_mode"),
        density: data.get("density"),
      });
      setStorage(APPEARANCE_KEY, JSON.stringify(next));
      applyAppearance(next);
    });
  }

  /**
   * LLM 接入预设：选中下拉框后把内置的编码/名称/基础 URL 填进创建表单。
   *
   * 下拉框在标题区、表单在下面的「Create resource」卡片里，所以这里按
   * action 找表单而不是用 select.form（那个值是 null）。只填非敏感字段：
   * API Key 永远由使用者自己粘贴，预设里没有、也不应该有密钥。
   */
  function bindLlmPresetPicker() {
    const selects = document.querySelectorAll("select[data-llm-preset]");
    if (!selects.length) return;
    const form = document.querySelector('form[action="/admin/llm"]');
    if (!form) return;
    for (const select of selects) {
      select.addEventListener("change", () => {
        const option = select.selectedOptions[0];
        // 「自定义」项没有 data-code：不动用户已经填好的内容。
        if (!option || !option.dataset.code) return;
        const values = {
          code: option.dataset.code,
          name: option.dataset.name || "",
          base_url: option.dataset.baseUrl || "",
        };
        for (const [field, value] of Object.entries(values)) {
          const control = form.elements.namedItem(field);
          if (control && "value" in control) control.value = value;
        }
        const apiKey = form.elements.namedItem("api_key");
        if (apiKey && typeof apiKey.focus === "function") apiKey.focus();
      });
    }
  }

  /**
   * LLM 端点的模型候选与连通性测试。
   *
   * 「获取模型列表」与「测试连接」都打 ``POST /admin/llm/probe``：前者只列
   * ``/models``，后者再发一次最小对话。表单里留空的 API Key 表示沿用库里
   * 已存的密钥——这一点由按钮上的 ``data-llm-endpoint`` 告诉服务端。
   */
  const PROBE_MESSAGES = {
    en: {
      fetching: "Fetching models…",
      testing: "Testing…",
      models: (count) => `${count} model${count === 1 ? "" : "s"} found`,
      passed: (ms, model) => `Passed · ${ms} ms · ${model || "ok"}`,
      limited: (detail) => `Reachable, chat limited · ${detail}`,
      failed: (detail) => `Failed · ${detail}`,
      network: "request failed",
    },
    "zh-CN": {
      fetching: "正在获取模型…",
      testing: "正在测试…",
      models: (count) => `已获取 ${count} 个模型`,
      passed: (ms, model) => `通过 · ${ms} ms · ${model || "ok"}`,
      limited: (detail) => `连接正常，对话受限 · ${detail}`,
      failed: (detail) => `失败 · ${detail}`,
      network: "请求失败",
    },
  };

  function probeMessages() {
    return document.documentElement.lang === "zh-CN"
      ? PROBE_MESSAGES["zh-CN"]
      : PROBE_MESSAGES.en;
  }

  function fillModelOptions(list, models) {
    if (!list) return;
    list.replaceChildren(
      ...models.map((id) => {
        const option = document.createElement("option");
        option.value = id;
        return option;
      }),
    );
  }

  function bindLlmProbe() {
    const buttons = document.querySelectorAll("[data-llm-fetch], [data-llm-test]");
    if (!buttons.length) return;
    for (const button of buttons) {
      const target = button.dataset.llmFetch || button.dataset.llmTest;
      const mode = button.dataset.llmTest ? "test" : "models";
      button.addEventListener("click", async () => {
        const form = button.form;
        if (!form) return;
        const list = document.getElementById(target);
        const status = document.querySelector(`[data-llm-status="${target}"]`);
        const text = probeMessages();
        const field = (name) => {
          const control = form.elements.namedItem(name);
          return control && "value" in control ? control.value : "";
        };
        const data = new FormData();
        data.set("mode", mode);
        data.set("code", button.dataset.llmEndpoint || "");
        data.set("base_url", field("base_url"));
        data.set("api_key", field("api_key"));
        data.set("model", field("model"));
        data.set("csrf_token", field("csrf_token"));
        button.disabled = true;
        if (status) {
          status.textContent = mode === "test" ? text.testing : text.fetching;
        }
        try {
          const response = await fetch("/admin/llm/probe", {
            method: "POST",
            body: data,
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || !payload.ok) {
            // 402/429 只说明账号额度/限流不够，地址与密钥其实已经验证过：
            // 列表照样回填，提示文案也换成「连接正常、对话受限」。
            fillModelOptions(
              list,
              Array.isArray(payload.models) ? payload.models : [],
            );
            if (status) {
              status.textContent = payload.limited
                ? text.limited(payload.error || "")
                : text.failed(payload.error || `HTTP ${response.status}`);
            }
            return;
          }
          const models = Array.isArray(payload.models) ? payload.models : [];
          fillModelOptions(list, models);
          const control = form.elements.namedItem("model");
          if (!field("model") && models.length && control && "value" in control) {
            // 候选有了但模型还空着：先填第一个，用户仍可改成任意值。
            control.value = models[0];
          }
          if (status) {
            status.textContent =
              mode === "test"
                ? text.passed(payload.latency_ms || 0, payload.model || "")
                : text.models(models.length);
          }
        } catch {
          if (status) status.textContent = text.failed(text.network);
        } finally {
          button.disabled = false;
        }
      });
    }
  }

  const locale = currentLocale();
  const appearance = readAppearance();
  applyAppearance(appearance);
  document.documentElement.lang = locale;

  document.addEventListener("DOMContentLoaded", () => {
    translateDocument(locale);
    bindLocaleSwitch(locale);
    bindAppearancePersistence(appearance);
    bindLoginControls();
    bindLlmPresetPicker();
    bindLlmProbe();
  });
})();
