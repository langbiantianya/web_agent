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
    // —— 2026-11-10 第三轮：把仍然漏翻的界面文案补齐 ——
    // 这些句子来自服务端渲染（Flash、字段级校验、注册表里的页面/组件
    // 描述），词表里没有对应条目时中文界面就会露出英文。
    "Resource restored.": "资源已恢复。",
    Cancel: "取消",
    "The connection could not be validated.": "连接无法校验。",
    "Priority must be a whole number.": "优先级必须是整数。",
    "Enter the HTTP URL for this server.": "请填写这台服务器的 HTTP 地址。",
    "Enter the stdio command for this server.":
      "请填写这台服务器的 stdio 命令。",
    "Timeout must be a whole number.": "超时时间必须是整数。",
    "The stdio command could not be started.": "stdio 命令无法启动。",
    "The HTTP URL could not be validated.": "HTTP 地址无法校验。",
    "The MCP server could not be reached, or its tool catalog could not be read. Check the URL or command and try again.":
      "连不上这台 MCP 服务器，或读不到它的工具目录。请检查地址或命令后重试。",
    "Some MCP tools are still used by Agents and cannot be removed from the catalog. Release them first, then refresh again.":
      "部分 MCP 工具仍被智能体使用，不能从目录中移除。请先解除占用，再刷新。",
    "Access mode: all": "访问模式：全部匹配",
    "Access mode: any": "访问模式：任一匹配",
    "Built-in role required by authentication defaults.":
      "内置角色，认证默认配置依赖它。",
    "Define which RBAC permissions make each registered page visible. Tenant overrides replace the global default.":
      "定义哪些 RBAC 权限让已注册的页面可见。租户覆盖会替换全局默认。",
    "Control tables, editors, actions and forms separately from page navigation. Hidden components never replace API authorization.":
      "分别控制表格、编辑器、操作与表单的可见性。隐藏组件不等于接口鉴权。",
    "Administration overview and appearance": "管理概览与外观",
    "Admin navigation and UI policy bindings": "管理导航与界面策略绑定",
    "OAuth administrator identities": "OAuth 管理员身份",
    "Agent configuration and connections": "智能体配置与连接",
    "Tool-call audit records": "工具调用审计记录",
    "Roles, policy rules and OAuth mappings": "角色、策略规则与 OAuth 映射",
    "Cross-tenant administration": "跨租户管理",
    "LLM endpoints and write-only credentials": "LLM 端点与只写凭据",
    "MCP servers, tools and catalog refresh": "MCP 服务器、工具与目录刷新",
    "OIDC provider configuration": "OIDC 身份提供方配置",
    "Platform super-administrator ownership": "平台超级管理员归属",
    "Skill drafts, publishing and archives": "技能草稿、发布与归档",
    "Workflow definitions and execution settings": "工作流定义与执行设置",
    "Component permission matrix": "组件权限矩阵",
    "Page permission matrix": "页面权限矩阵",
    "Role policy matrix": "角色策略矩阵",
    "Agent list": "智能体列表",
    "Agent editor": "智能体编辑器",
    "Tool audit list": "工具审计列表",
    "Platform metrics": "平台指标",
    "Platform admin accounts": "平台管理员账号",
    "Theme and appearance": "主题与外观",
    "Skill list": "技能列表",
    "Workflow list": "工作流列表",
    "MCP server list": "MCP 服务器列表",
    "LLM endpoint list": "LLM 端点列表",
    "OAuth provider list": "身份提供方列表",
    "Quick actions": "快捷操作",
    // —— 本轮新增的界面文案（独立编辑面板、能力工具条、列表控件）——
    "ACCESS CONTROL": "访问控制",
    "IDENTITY MAPPING": "身份映射",
    "Roles & permissions": "角色与权限",
    "Manage reusable Casbin roles, permission matrices and inheritance. Platform super admins bypass these rules.":
      "管理可复用的 Casbin 角色、权限矩阵和继承关系。平台超级管理员不受这些规则限制。",
    "Role code": "角色编码",
    "Domain": "作用域",
    "Create role": "创建角色",
    "Edit permissions": "编辑权限",
    "Save permissions": "保存权限",
    "Role inheritance": "角色继承",
    "No parent roles": "无父角色",
    "Save inheritance": "保存继承",
    "Search roles": "搜索角色",
    "Role, permission or scope": "角色、权限或作用域",
    "No roles found": "没有找到角色",
    "Create a role or adjust the current search.":
      "请创建角色或调整当前搜索条件。",
    "Permission catalog": "权限目录",
    "Permission policy": "权限策略",
    "Recommended": "推荐权限",
    "No recommendation": "无推荐权限",
    "Global default": "全局默认",
    "Tenant override": "租户覆盖",
    "Add tenant override": "新增租户覆盖",
    "Tenant ID": "租户 ID",
    "Create override": "创建覆盖",
    "Reset to global": "重置为全局默认",
    "No permissions are bound.": "尚未绑定权限。",
    "No default": "无全局默认",
    "Default set": "已设置全局默认",
    "tenant overrides": "个租户覆盖",
    "Access mode": "访问模式",
    "Select permissions from groups below. API routes still perform their own authorization checks; page visibility is not an API boundary.":
      "从下方权限组中勾选。API 路由仍会独立执行授权检查，页面可见性不是 API 权限边界。",
    "Tenant overrides replace the global binding while they exist. Use reset instead of saving an empty tenant selection.":
      "租户覆盖存在时会替换全局绑定。需要回退时请使用“重置为全局默认”，不要保存空租户权限。",
    "Choose permissions or clear to remove this override":
      "选择权限；清空可移除当前覆盖",
    "Save scope": "保存作用域",
    "Role and status": "角色与状态",
    "Built-in role": "内置角色",
    "Custom role": "自定义角色",
    "No permissions": "无权限",
    "No other role exists in this domain yet.":
      "该作用域下还没有其他角色。",
    "Map exact external claim values to managed roles. Updates apply on the administrator's next OAuth synchronization.":
      "将外部声明的精确值映射到受管角色。更改会在管理员下一次 OAuth 同步时生效。",
    "roles": "个角色",
    "permission grants": "条权限授权",
    "scopes": "个作用域",
    "components": "个组件",
    "grants": "条授权",
    "pages": "个页面",
    "Edit agent": "编辑智能体",
    "Edit skill": "编辑技能",
    "Edit endpoint": "编辑端点",
    "Edit connection": "编辑连接",
    "Edit provider": "编辑身份提供方",
    Close: "关闭",
    "Search tools": "搜索工具",
    "Tool name or description": "工具名称或描述",
    "Only available": "只看可用",
    "0 selected": "已选 0 项",
    "Suggested tools:": "建议工具：",
    "No tool suggestions": "没有工具建议",
    "Skill tool suggestions": "技能工具建议",
    Troubleshooting: "排查指引",
    "Check the base URL, the API key and whether the service accepts the OpenAI-compatible protocol (POST /v1/chat/completions).":
      "请检查基础 URL、API 密钥，以及该服务是否接受 OpenAI 兼容协议（POST /v1/chat/completions）。",
    "A 401 means the key is wrong, a 404 means the base URL is wrong, and a timeout means the host is unreachable.":
      "401 说明密钥不对，404 说明基础 URL 不对，超时说明连不上主机。",
    Apply: "应用",
    "Undo delete": "撤销删除",
    "Agent was deleted.": "智能体已删除。",
    "LLM endpoint was deleted.": "LLM 端点已删除。",
    "MCP server was deleted.": "MCP 服务器已删除。",
    "Workflow was deleted.": "工作流已删除。",
    "Platform admin was deleted.": "平台管理员已删除。",
    Search: "搜索",
    "Search rules": "搜索规则",
    "Search accounts": "搜索账户",
    "Name or code": "名称或编码",
    Newest: "最新",
    "Type, subject or role": "类型、主体或角色",
    "Username or email": "用户名或邮箱",
    "Sort by": "排序方式",
    Default: "默认",
    Previous: "上一页",
    Next: "下一页",
    "Published Skills": "已发布技能",
    "Built-in tools": "内置工具",
    "Unavailable bindings": "失效绑定",
    "The skill could not be saved. Fix the highlighted fields.":
      "技能未能保存，请修正高亮字段。",
    "The endpoint could not be saved. Fix the highlighted fields.":
      "端点未能保存，请修正高亮字段。",
    "The connection could not be saved. Fix the highlighted fields.":
      "连接未能保存，请修正高亮字段。",
    "The agent could not be saved. Fix the highlighted fields.":
      "智能体未能保存，请修正高亮字段。",
    "The provider could not be saved. Fix the highlighted fields.":
      "身份提供方未能保存，请修正高亮字段。",
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
    "Bind published Skills and discovered tools. Skill tool hints are suggestions only; tool access is granted by the checked tools below.":
      "绑定已发布的技能和已发现的工具。技能中的工具提示仅供参考，实际权限只取决于下方勾选的工具。",
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
    "Capabilities": "能力",
    "Capabilities are managed on": "能力配置位于",
    "and": "和",
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
    "View graph": "查看流程图",
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
    "LLM connection": "LLM 连接",
    "Login scopes": "登录 Scope",
    "Manage graph definitions directly against the shared workflow repository.":
      "直接针对共享工作流仓库管理图定义。",
    "Back to list": "返回列表",
    Undo: "撤销",
    Redo: "重做",
    "Auto layout": "自动布局",
    "Fit canvas": "适应画布",
    "Node palette": "节点面板",
    "Agent node": "智能体节点",
    "Approval node": "审批节点",
    "Available agents": "可用智能体",
    Inspector: "属性面板",
    "Save workflow": "保存工作流",
    "Manage platform configuration, access policy, and operations.":
      "管理平台配置、访问策略和运维操作。",
    MCP: "MCP",
    "MCP servers": "MCP 服务器",
    Menu: "菜单",
    Model: "模型",
    "Missing LLM connection": "LLM 连接缺失",
    Name: "名称",
    No: "否",
    "No OAuth provider is configured.": "尚未配置 OAuth 身份提供方。",
    None: "无",
    "No LLM connections configured": "尚未配置 LLM 连接",
    "No LLM connection selected": "未选择 LLM 连接",
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
    "Configure OIDC once and choose whether each provider may authenticate client users, administrators, or both. Client secrets are write-only.":
      "只需配置一次 OIDC，即可选择各提供方用于客户端用户、管理员或两者登录。客户端密钥始终只写。",
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
    "Only superAdmin can add or delete these role-less platform accounts. Passwords are generated and delivered automatically.":
      "只有 superAdmin 可以新增或删除这些无角色平台账户。密码会自动生成并发送。",
    Preferences: "偏好设置",
    Priority: "优先级",
    "Prompt, model, tool, and skill bindings are managed inside the isolated admin service.":
      "提示词、模型、工具和技能绑定由独立管理服务维护。",
    "Each Agent selects a configured LLM connection. Base URLs and API keys are maintained once on the LLM endpoints page.":
      "每个智能体选择一条已配置的 LLM 连接。基础 URL 和 API 密钥统一在 LLM 端点页面维护。",
    "Each Agent selects a configured LLM connection and explicit Skills and tools. Tool hints from a Skill remain suggestions; only checked tools are authorized.":
      "每个智能体选择已配置的 LLM 连接，并明确绑定技能和工具。技能中的工具提示仅供参考，只有勾选的工具才会获得授权。",
    Provider: "身份提供方",
    "Provider code": "身份提供方代码",
    "Provider enabled": "启用身份提供方",
    "Provider preset": "服务商预设",
    Published: "已发布",
    published: "已发布",
    "Published Skills": "已发布技能",
    "Built-in tools": "内置工具",
    "Unavailable bindings": "失效绑定",
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
    "Preferences saved.": "偏好设置已保存。",
    "The submitted values are invalid.": "提交的值无效。",
    "A resource with that code already exists.": "该代码对应的资源已存在。",
    "The requested resource no longer exists.": "请求的资源已不存在。",
    "This resource is in use and cannot be changed.": "该资源正在被使用，当前不能修改。",
    "This Agent is referenced by one or more workflows.": "该智能体仍被一个或多个工作流引用。",
    "The owner account cannot be deleted.": "平台所有者账户不能删除。",
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
    "Select a configured LLM connection": "选择已配置的 LLM 连接",
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
    "Suggested tools": "建议工具",
    "Suggested tools (comma separated)": "建议工具（逗号分隔）",
    "Soft elevation, expressive shapes, and Google blue actions.":
      "柔和层级、表现力形状和 Google 蓝色操作。",
    Source: "来源",
    State: "状态",
    Status: "状态",
    Success: "成功",
    System: "跟随系统",
    system: "跟随系统",
    "System prompt": "系统提示词",
    "Uses the LLM connection default when blank": "留空时使用 LLM 连接的默认模型",
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
    "Built-in tool": "内置工具",
    Tools: "工具",
    "Open editor": "打开编辑器",
    "Initial agent": "初始智能体",
    Transport: "传输方式",
    Type: "类型",
    URL: "URL",
    "Upsert scope": "写入 Scope",
    "Use your organization identity. Roles are synchronized from OAuth claims.":
      "使用组织身份登录，角色通过 OAuth 声明同步。",
    Username: "用户名",
    "Username claim": "用户名字段",
    "Used by agents": "被智能体使用",
    "Used by workflows": "被工作流使用",
    "no default model": "无默认模型",
    "No capabilities bound": "尚未绑定能力",
    "Unavailable binding — uncheck to remove": "绑定已失效，取消勾选即可移除",
    "Refresh to discover tools": "刷新以发现工具",
    Version: "版本",
    Workflow: "工作流",
    Workflows: "工作流",
    "Legacy / manual": "旧版 / 手动",
    "Configure an LLM connection before creating an Agent.":
      "创建智能体前请先配置 LLM 连接。",
    "Open LLM endpoints": "打开 LLM 端点",
    "Your administrator account is active, but all dashboard components are hidden by the current UI policy.":
      "管理员账户已启用，但当前界面策略隐藏了所有仪表盘组件。",
    Yes: "是",
    owner: "所有者",
    active: "启用",
  };

  const ZH_PATTERNS = [
    // pydantic 的原始报错前缀：中文界面里露出 "Value error," 很突兀。
    // 注意：模式是**第一条命中就生效**，所以带具体译法的那条必须排在
    // 通用剥离规则前面。
    [
      /^Value error, base_url 必须是 http\(s\) 地址$/,
      "基础 URL 必须是 http(s) 地址",
    ],
    [/^Value error, (.+)$/, "$1"],
    [/^(.+) · Workflow editor$/, "$1 · 工作流编辑器"],
    [/^(\d+) item\(s\) · page (\d+) of (\d+)$/, "共 $1 项 · 第 $2 / $3 页"],
    [/^(\d+) members$/, "$1 个成员"],
    [/^(\d+) permissions$/, "$1 项权限"],
    [/^(\d+) components$/, "$1 个组件"],
    [/^(\d+) roles$/, "$1 个角色"],
    [/^(\d+) permission grants$/, "$1 条权限授权"],
    [/^(\d+) scopes$/, "$1 个作用域"],
    [/^(\d+) pages$/, "$1 个页面"],
    [/^(\d+) grants$/, "$1 条授权"],
    [/^(\d+) tenant overrides$/, "$1 个租户覆盖"],
    [/^v(\d+) · published$/, "v$1 · 已发布"],
    [/^v(\d+) · draft$/, "v$1 · 草稿"],
    [/^v(\d+) · archived$/, "v$1 · 已归档"],
    [/^v(\d+) · published · disabled$/, "v$1 · 已发布 · 已禁用"],
    [/^v(\d+) · draft · disabled$/, "v$1 · 草稿 · 已禁用"],
    [/^(\d+) agent\(s\)$/, "$1 个智能体"],
    [/^provider #(\d+)$/, "身份提供方 #$1"],
    [/^Continue with (.+)$/, "使用 $1 继续"],
    [/^New scope permissions for (.+)$/, "为 $1 设置新的 Scope 权限"],
    [/^New tenant scope for (.+)$/, "为 $1 设置新的租户范围"],
    [/^Tenant scope for (.+)$/, "租户范围：$1"],
    [/^(.+) sync age$/, "$1 同步时效"],
    [
      /^Delete LLM endpoint (.+)\? This action cannot be undone\.$/,
      "删除 LLM 端点 $1？此操作无法撤销。",
    ],
    [
      /^Delete MCP server (.+)\? This action cannot be undone\.$/,
      "删除 MCP 服务器 $1？此操作无法撤销。",
    ],
    [
      /^Delete Agent (.+)\? This action cannot be undone\.$/,
      "删除智能体 $1？此操作无法撤销。",
    ],
    [
      /^Delete Workflow (.+)\? This action cannot be undone\.$/,
      "删除工作流 $1？此操作无法撤销。",
    ],
    [
      /^Delete platform admin (.+)\? This action cannot be undone\.$/,
      "删除平台管理员 $1？此操作无法撤销。",
    ],
    [
      /^Delete OAuth provider (.+)\? This action cannot be undone\.$/,
      "删除 OAuth 身份提供方 $1？此操作无法撤销。",
    ],
    [/^Delete (.+)\? This action cannot be undone\.$/, "删除 $1？此操作无法撤销。"],
    // —— 删除确认对话框：标题与影响摘要在运行时注入，这里按模式翻译 ——
    [/^Delete LLM endpoint (.+)$/, "删除 LLM 端点 $1"],
    [/^Delete MCP server (.+)$/, "删除 MCP 服务器 $1"],
    [/^Delete Agent (.+)$/, "删除智能体 $1"],
    [/^Delete Workflow (.+)$/, "删除工作流 $1"],
    [/^Delete role (.+)$/, "删除角色 $1"],
    [/^Delete platform admin (.+)$/, "删除平台管理员 $1"],
    [/^Delete OAuth provider (.+)$/, "删除 OAuth 身份提供方 $1"],
    [/^Archive Skill (.+)$/, "归档技能 $1"],
    // —— 编辑面板标题与撤销按钮的可访问名称 ——
    [/^Edit endpoint (.+)$/, "编辑端点 $1"],
    [/^Edit agent (.+)$/, "编辑智能体 $1"],
    [/^Edit skill (.+)$/, "编辑技能 $1"],
    [/^Edit connection (.+)$/, "编辑连接 $1"],
    [/^Edit provider (.+)$/, "编辑身份提供方 $1"],
    [/^Undo delete (.+)$/, "撤销删除 $1"],
    [/^Delete (.+)$/, "删除 $1"],
    // —— 删除确认对话框的影响摘要（运行时注入）——
    [
      /^Agent (.+) \((.+)\) will be permanently removed\.$/,
      "智能体 $1（$2）将被永久删除。",
    ],
    [
      /^LLM endpoint (.+) \((.+)\) and its stored API key will be permanently removed\.$/,
      "LLM 端点 $1（$2）及其已存 API 密钥将被永久删除。",
    ],
    [
      /^MCP server (.+) \((.+)\) and its discovered tool catalog will be permanently removed\.$/,
      "MCP 服务器 $1（$2）及其已发现的工具目录将被永久删除。",
    ],
    [
      /^Skill (.+) \((.+)\) will be archived and removed from active use\.$/,
      "技能 $1（$2）将被归档，不再参与运行。",
    ],
    [
      /^Workflow (.+) \((.+)\) will be permanently removed\.$/,
      "工作流 $1（$2）将被永久删除。",
    ],
    [
      /^Agents keep their configuration; the agent itself is gone\.$/,
      "智能体的配置会保留，但智能体本身已删除。",
    ],
    [
      /^Skills keep their resources and revisions; restore is available from the API\.$/,
      "技能会保留资源与修订，可通过 API 恢复。",
    ],
    [
      /^Runs already started keep their in-memory definition snapshot\.$/,
      "已开始的运行会保留内存中的定义快照。",
    ],
    [/^No workflow references this agent\.$/, "没有工作流引用该智能体。"],
    [/^No agent uses this endpoint\.$/, "没有智能体使用该端点。"],
    [/^Used by agents: (.+)$/, "被智能体使用：$1"],
    [/^Referenced by workflows: (.+)$/, "被工作流引用：$1"],
    [/^(\d+) discovered tool\(s\) will be removed\.$/, "将删除 $1 个已发现的工具。"],
    // —— 平台超级管理员的删除确认与撤销提示 ——
    [
      /^Platform admin (.+) will be removed and its sessions revoked\.$/,
      "平台管理员 $1 将被删除，其会话立即失效。",
    ],
    [
      /^The account loses access to the console immediately\.$/,
      "该账号将立即失去后台访问权限。",
    ],
    [
      /^The account can be restored from the undo entry\.$/,
      "该账号可以从撤销入口恢复。",
    ],
    [/^Delete LLM endpoint (.+)$/, "删除 LLM 端点 $1"],
    [/^Delete MCP server (.+)$/, "删除 MCP 服务器 $1"],
    [/^Delete Agent (.+)$/, "删除智能体 $1"],
    [/^Delete Workflow (.+)$/, "删除工作流 $1"],
    [
      /^Archive Skill (.+)\? The skill will be removed from active use\.$/,
      "归档技能 $1？该技能将不再参与新的绑定。",
    ],
    [/^Disable LLM endpoint (.+)$/, "禁用 LLM 端点 $1"],
    [/^Enable LLM endpoint (.+)$/, "启用 LLM 端点 $1"],
    [/^Disable MCP server (.+)$/, "禁用 MCP 服务器 $1"],
    [/^Enable MCP server (.+)$/, "启用 MCP 服务器 $1"],
    [/^Disable Agent (.+)$/, "禁用智能体 $1"],
    [/^Enable Agent (.+)$/, "启用智能体 $1"],
    [/^Disable Workflow (.+)$/, "禁用工作流 $1"],
    [/^Enable Workflow (.+)$/, "启用工作流 $1"],
    [/^Disable OAuth provider (.+)$/, "禁用 OAuth 身份提供方 $1"],
    [/^Enable OAuth provider (.+)$/, "启用 OAuth 身份提供方 $1"],

    [/^Archive Skill (.+)$/, "归档技能 $1"],
    [/^Used by agents: (.+)$/, "被智能体使用：$1"],
    [/^Used by workflows: (.+)$/, "被工作流使用：$1"],
    [/^Referenced by workflows: (.+)$/, "被工作流引用：$1"],
    [/^(\d+) skills · (\d+) tools$/, "$1 个技能 · $2 个工具"],
    [/^(\d+) tools$/, "$1 个工具"],
    [
      /^(\d+) redacted tool invocation records are visible to this tenant\.$/,
      "当前租户有 $1 条脱敏工具调用记录。",
    ],
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
    for (const name of ["aria-label", "title", "placeholder", "data-confirm"]) {
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
   *
   * **为什么不能只按 action 找**：列表上方的「搜索 / 排序」过滤表单和
   * 创建表单的 action 都是 ``/admin/llm``（前者是 GET，后者是 POST）。
   * ``querySelector`` 只会命中排在文档里的第一个——过滤表单排在前面，
   * 于是预设把值写进了过滤表单的 ``q``/``sort`` 之外的名字上，一个字段
   * 也填不到，用户看到的就是「选了预设没反应」。所以这里按**有没有
   * ``code`` 字段**认出创建表单：只有创建表单才有「代码」这一栏。
   */
  function bindLlmPresetPicker() {
    const selects = document.querySelectorAll("select[data-llm-preset]");
    if (!selects.length) return;
    const form = [...document.querySelectorAll('form[action="/admin/llm"]')].find(
      (candidate) => candidate.elements.namedItem("code"),
    );
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

  function bindAgentConnectionDefaults() {
    for (const select of document.querySelectorAll(
      'select[name="llm_endpoint_code"]',
    )) {
      const form = select.form;
      const model = form?.elements.namedItem("model");
      if (!form || !model || !("placeholder" in model)) continue;
      const genericPlaceholder =
        model.dataset.agentModelPlaceholder || model.placeholder;
      model.dataset.agentModelPlaceholder = genericPlaceholder;
      const updateHint = () => {
        const option = select.selectedOptions[0];
        const label = option?.textContent || "";
        const marker = label.lastIndexOf(" · ");
        const fallback = marker >= 0 ? label.slice(marker + 3).trim() : "";
        const noDefault =
          fallback === "no default model" || fallback === "无默认模型";
        const zh = document.documentElement.lang === "zh-CN";
        if (!select.value) {
          model.placeholder = genericPlaceholder;
          model.required = false;
        } else if (fallback && !noDefault) {
          model.placeholder = zh
            ? `留空时使用 ${fallback}`
            : `Uses ${fallback} when blank`;
          model.required = false;
        } else {
          model.placeholder = zh
            ? "该连接没有默认模型，请填写模型"
            : "This connection has no default model; enter one";
          model.required = true;
        }
      };
      select.addEventListener("change", updateHint);
      updateHint();
    }
  }

  function bindMcpTransportFields() {
    for (const select of document.querySelectorAll(
      'select[name="transport"]',
    )) {
      const form = select.form;
      const url = form?.elements.namedItem("url");
      const command = form?.elements.namedItem("command");
      const updateFields = () => {
        const stdio = select.value === "stdio";
        for (const [control, visible] of [
          [url, !stdio],
          [command, stdio],
        ]) {
          if (!control || !("disabled" in control)) continue;
          const label = control.closest("label");
          if (label) label.hidden = !visible;
          control.disabled = !visible;
          control.required = visible;
        }
      };
      select.addEventListener("change", updateFields);
      updateFields();
    }
  }

  /**
   * LLM 端点的模型候选与连通性测试。
   *
   * 「获取模型列表」与「测试连接」都打 ``POST /admin/llm/probe``：前者只列
   * ``/models``，后者再发一次最小对话。表单里留空的 API Key 表示沿用库里
   * 已存的密钥——这一点由按钮上的 ``data-llm-endpoint`` 告诉服务端。
   */
  // 探测失败的**本地化说明**：键与服务端返回的 ``error_code`` 对应。
  //
  // 服务端只给稳定分类 + 英文原文；中文界面按分类显示中文，原文只留在
  // 英文界面与审计里（UX-37）。
  const PROBE_ERRORS = {
    "zh-CN": {
      http_400: "端点拒绝了这次请求，请检查模型名。",
      http_401: "API 密钥被拒绝，请检查密钥及其所属账号。",
      http_402: "该端点的账号没有剩余额度。",
      http_403: "端点拒绝了这次请求，密钥可能缺少所需权限。",
      http_404: "路径不存在，请检查基础 URL。",
      http_408: "端点超时。",
      http_429: "端点正在限流这个密钥，请稍后再试。",
      http_500: "端点返回了服务端错误。",
      http_502: "上游网关返回了无效响应。",
      http_503: "端点暂时不可用。",
      http_504: "上游网关超时。",
      network: "连不上端点，请检查基础 URL 与网络。",
      invalid_response: "端点返回了无法解析的响应。",
      no_models: "端点没有返回任何模型。",
      model_required: "请先填写模型名。",
      invalid_base_url: "基础 URL 不是合法的 http(s) 地址。",
      unknown: "探测失败。",
    },
    en: {
      http_400: "The endpoint rejected the request. Check the model name.",
      http_401: "The API key was rejected. Check the key and its account.",
      http_402: "The account has no remaining credit for this endpoint.",
      http_403: "The endpoint refused the request. The key may lack a scope.",
      http_404: "The path was not found. Check the base URL.",
      http_408: "The endpoint timed out.",
      http_429: "The endpoint is rate limiting this key. Try again later.",
      http_500: "The endpoint returned a server error.",
      http_502: "The upstream gateway returned an invalid response.",
      http_503: "The endpoint is temporarily unavailable.",
      http_504: "The upstream gateway timed out.",
      network: "Could not reach the endpoint. Check the base URL and network.",
      invalid_response: "The endpoint returned an unparsable response.",
      no_models: "The endpoint returned no models.",
      model_required: "Fill in a model name first.",
      invalid_base_url: "The base URL is not a valid http(s) URL.",
      unknown: "The probe failed.",
    },
  };

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

  // 把服务端的错误分类翻成本地化说明。
  //
  // 英文界面保留服务端原文（那是给英文管理员看的细节）；中文界面按稳定
  // 分类给出中文说明，避免在中文页面里贴一整句英文（UX-37）。
  function probeErrorMessage(code, fallback) {
    const table =
      document.documentElement.lang === "zh-CN" ? PROBE_ERRORS["zh-CN"] : PROBE_ERRORS.en;
    if (code && Object.hasOwn(table, code)) return table[code];
    return fallback || table.unknown;
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
      const help = document.querySelector(`[data-llm-help="${target}"]`);
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
        button.setAttribute("aria-busy", "true");
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
              const detail = probeErrorMessage(
                payload.error_code,
                payload.error || `HTTP ${response.status}`,
              );
              status.textContent = payload.limited
                ? text.limited(detail)
                : text.failed(detail);
              // 失败时才给出排查指引（UX-07）。
              if (help) help.hidden = false;
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
          if (help) help.hidden = true;
        } catch {
          if (status) status.textContent = text.failed(text.network);
          if (help) help.hidden = false;
        } finally {
          button.disabled = false;
          button.setAttribute("aria-busy", "false");
        }
      });
    }
  }

  /**
   * 删除确认对话框。
   *
   * 行内的删除按钮只是**触发器**（``button[data-confirm-trigger]``），
   * 它自己不提交表单——真正的提交发生在页面级对话框里的表单上。
   * 这样「单击立即提交」变成「单击 → 阅读影响摘要 → 确认」。
   *
   * 对话框内容从触发器的 data 属性搬运：标题、说明、影响摘要。
   * 表单的 action 与 CSRF 从触发器**所在的表单**里取，删除请求因此
   * 与直接提交完全一致。
   */
  function bindConfirmDialog() {
    const dialog = document.querySelector("[data-confirm-dialog]");
    if (!dialog) return;
    const titleNode = dialog.querySelector("#confirm-dialog-title");
    const summaryNode = dialog.querySelector("[data-confirm-dialog-summary]");
    const impactsNode = dialog.querySelector("[data-confirm-dialog-impacts]");
    const form = dialog.querySelector("form");
    const csrfInput = form?.querySelector('input[name="csrf_token"]');
    const submitButton = dialog.querySelector("[data-confirm-dialog-submit]");
    let pending = null;

    const close = () => {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
      pending = null;
    };

    for (const button of document.querySelectorAll(
      "button[data-confirm-trigger]",
    )) {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        if (button.disabled) return;
        const sourceForm = button.closest("form");
        if (!sourceForm || !form) return;
        pending = { source: sourceForm };
        const locale = document.documentElement.lang === "zh-CN"
          ? "zh-CN"
          : "en";
        const tr = (value) => translatedText(String(value || ""), locale);
        if (titleNode) {
          titleNode.textContent = tr(
            button.dataset.confirmTitle || "Confirm",
          );
        }
        if (summaryNode) {
          summaryNode.textContent = tr(button.dataset.confirmSummary || "");
        }
        // 提交按钮的文案：触发器可以指定（「归档」不要写成「删除」）。
        if (submitButton) {
          const label = button.dataset.confirmSubmitLabel || "Delete";
          submitButton.textContent = tr(label);
        }
        if (impactsNode) {
          impactsNode.replaceChildren(
            ...String(button.dataset.confirmImpacts || "")
              .split("\n")
              .filter((line) => line.trim().length > 0)
              .map((line) => {
                const item = document.createElement("p");
                item.className = "confirm-impact";
                item.textContent = tr(line.trim());
                return item;
              }),
          );
        }
        const sourceCsrf = sourceForm.querySelector(
          'input[name="csrf_token"]',
        );
        if (csrfInput && sourceCsrf) {
          csrfInput.value = sourceCsrf.value;
        }
        form.action = sourceForm.getAttribute("action") || "";
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
        if (submitButton) submitButton.focus();
      });
    }

    for (const button of dialog.querySelectorAll(
      "[data-confirm-dialog-close]",
    )) {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        close();
      });
    }

    dialog.addEventListener("cancel", (event) => {
      // Esc 关闭：默认行为已经会关掉弹层，这里只需要清掉待提交状态。
      event.preventDefault();
      close();
    });

    // 对话框不是原生 ``<dialog>``，所以自己处理 Esc。
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!dialog.hasAttribute("open")) return;
      event.preventDefault();
      close();
    });

    form?.addEventListener("submit", (event) => {
      if (!pending) {
        event.preventDefault();
        return;
      }
      // 表单照常提交；pending 只用于避免「没打开过对话框也能提交」。
    });
  }

  /**
   * 能力选择器的工具条（UX-19）。
   *
   * 工具多的时候整屏铺开，找一个工具要翻很久。这里提供三件事：
   *
   * - **本地搜索**：按工具名 / 描述过滤，空分组自动隐藏；
   * - **只看可用**：把「服务器已停用」的条目过滤掉；
   * - **已选计数**：实时显示勾选数量（含 Skill 与工具）。
   *
   * 分组标题还可以点击折叠，服务器一多时页面依然是可扫读的。
   */
  function bindCapabilitySelector() {
    for (const selector of document.querySelectorAll(
      "[data-capability-selector]",
    )) {
      const search = selector.querySelector("[data-capability-search]");
      const onlyAvailable = selector.querySelector(
        "[data-capability-available-only]",
      );
      const counter = selector.querySelector("[data-capability-count]");
      const groups = Array.from(
        selector.querySelectorAll("[data-capability-group]"),
      );
      const choices = Array.from(
        selector.querySelectorAll(".capability-choice"),
      );
      const suggestions = Array.from(
        selector.querySelectorAll("[data-capability-suggestion-tool]"),
      );

      const matches = (choice) => {
        const keyword = (search?.value || "").trim().toLowerCase();
        if (keyword) {
          const haystack = (
            choice.dataset.capabilityTitle || choice.textContent || ""
          ).toLowerCase();
          if (!haystack.includes(keyword)) return false;
        }
        if (onlyAvailable?.checked) {
          if (choice.dataset.capabilityUnavailable === "1") return false;
        }
        return true;
      };

      const apply = () => {
        for (const choice of choices) {
          choice.hidden = !matches(choice);
        }
        for (const group of groups) {
          const visible = group.querySelectorAll(
            ".capability-choice:not([hidden])",
          );
          group.hidden = visible.length === 0;
          if (visible.length === 0) {
            group.classList.add("is-empty");
          } else {
            group.classList.remove("is-empty");
          }
        }
        if (counter) {
          const selected = choices.filter(
            (choice) =>
              choice.querySelector("input[type=checkbox]")?.checked,
          ).length;
          const zh = document.documentElement.lang === "zh-CN";
          counter.textContent = zh
            ? `已选 ${selected} 项`
            : `${selected} selected`;
          counter.dataset.capabilitySelected = String(selected);
        }
        const selectedTools = new Set(
          Array.from(
            selector.querySelectorAll(
              'input[name="tools"]:checked',
            ),
          ).map((input) => input.value),
        );
        const zh = document.documentElement.lang === "zh-CN";
        const labels = zh
          ? {
              granted: "已授予当前智能体",
              disabled: "服务器已停用",
              missing: "工具不存在",
              "not-granted": "未授予当前智能体",
            }
          : {
              granted: "granted to this agent",
              disabled: "server is disabled",
              missing: "tool does not exist",
              "not-granted": "not granted to this agent",
            };
        for (const suggestion of suggestions) {
          const toolName = suggestion.dataset.capabilitySuggestionTool || "";
          const original =
            suggestion.dataset.capabilitySuggestionStatus || "not-granted";
          const status = selectedTools.has(toolName)
            ? "granted"
            : original === "disabled" || original === "missing"
              ? original
              : "not-granted";
          for (const value of [
            "granted",
            "disabled",
            "missing",
            "not-granted",
          ]) {
            suggestion.classList.toggle(`is-${value}`, value === status);
          }
          suggestion.textContent = `${toolName} · ${labels[status]}`;
          suggestion.title = labels[status];
        }
      };

      search?.addEventListener("input", apply);
      onlyAvailable?.addEventListener("change", apply);
      selector.addEventListener("change", apply);

      for (const toggle of selector.querySelectorAll(
        "[data-capability-toggle]",
      )) {
        toggle.addEventListener("click", () => {
          const group = toggle.closest("[data-capability-group]");
          if (!group) return;
          const collapsed = group.classList.toggle("is-collapsed");
          toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
        });
      }

      apply();
    }
  }

  /**
   * 独立编辑面板（UX-09）。
   *
   * 面板由 URL 上的 ``?resource=<code>`` 决定，因此：
   *
   * - 深链接打开后自动滚到面板并聚焦标题；
   * - 面板里的表单一旦被改动，离开页面前会提示「未保存」。
   */
  function bindEditPanels() {
    for (const panel of document.querySelectorAll("[data-edit-panel]")) {
      const form = panel.querySelector("form");
      if (!form) continue;
      let dirty = false;
      const markDirty = () => {
        if (dirty) return;
        dirty = true;
        panel.dataset.editDirty = "1";
      };
      const markClean = () => {
        dirty = false;
        delete panel.dataset.editDirty;
      };
      form.addEventListener("input", markDirty);
      form.addEventListener("change", markDirty);
      form.addEventListener("submit", markClean);

      for (const link of panel.querySelectorAll("[data-edit-close]")) {
        link.addEventListener("click", (event) => {
          if (!dirty) return;
          const zh = document.documentElement.lang === "zh-CN";
          const message = zh
            ? "编辑内容尚未保存，确定要离开吗？"
            : "You have unsaved changes. Leave anyway?";
          if (!window.confirm(message)) {
            event.preventDefault();
          }
        });
      }
    }

    let warned = false;
    window.addEventListener("beforeunload", (event) => {
      const dirtyPanel = document.querySelector("[data-edit-dirty]");
      if (!dirtyPanel || warned) return;
      warned = true;
      event.preventDefault();
      event.returnValue = "";
    });

    const panel = document.querySelector("[data-edit-panel]");
    if (panel && panel.dataset.editPanel) {
      panel.scrollIntoView({ block: "start" });
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
    bindAgentConnectionDefaults();
    bindMcpTransportFields();
    bindLlmProbe();
    bindConfirmDialog();
    bindCapabilitySelector();
    bindEditPanels();
  });
})();
