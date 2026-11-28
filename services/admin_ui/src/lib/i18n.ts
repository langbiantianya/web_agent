/**
 * 界面词典。
 *
 * **为什么不再用「运行时遍历 DOM 翻译」**：旧实现（`admin-ui.js` 的
 * `translateDocument`）在浏览器里遍历文本节点、逐个查表替换。它的问题不是
 * 慢，而是**脆**：文案一旦出现在属性、标题或第三方组件里就会漏翻，而且
 * 翻译发生在渲染之后，SSR 与客户端的首帧可能不一致。这里改成**构建期
 * 固定的双语词典**，文案在组件里显式取用，漏翻在类型检查和快照测试里
 * 就能发现。
 */

/** 支持的语言。 */
export const LOCALES = ['en', 'zh-CN'] as const;

/** 语言代码。 */
export type Locale = (typeof LOCALES)[number];

/** 默认语言。 */
export const DEFAULT_LOCALE: Locale = 'en';

/** 词典键。 */
export type MessageKey =
	| 'app.title'
	| 'nav.group.overview'
	| 'nav.group.access'
	| 'nav.group.resources'
	| 'nav.group.security'
	| 'nav.group.settings'
	| 'nav.group.operations'
	| 'nav.overview'
	| 'nav.access.pages'
	| 'nav.access.components'
	| 'nav.access.roles'
	| 'nav.access.mappings'
	| 'nav.workflows'
	| 'nav.agents'
	| 'nav.skills'
	| 'nav.mcp'
	| 'nav.llm'
	| 'nav.oauth'
	| 'nav.platformAdmins'
	| 'nav.preferences'
	| 'nav.audit'
	| 'shell.language'
	| 'shell.signOut'
	| 'dashboard.title'
	| 'dashboard.platformOverview'
	| 'dashboard.conversations'
	| 'dashboard.runs'
	| 'dashboard.activeAdmins'
	| 'dashboard.platformAdmins'
	| 'dashboard.quickActions'
	| 'dashboard.editPagePermissions'
	| 'dashboard.editComponentPermissions'
	| 'dashboard.reviewCasbinRoles'
	| 'dashboard.empty'
	| 'common.search'
	| 'common.sort'
	| 'common.apply'
	| 'common.prev'
	| 'common.next'
	| 'common.total'
	| 'common.actions'
	| 'common.enabled'
	| 'common.disabled'
	| 'common.code'
	| 'common.name'
	| 'common.model'
	| 'common.created'
	| 'common.empty'
	| 'common.save'
	| 'common.cancel'
	| 'common.create'
	| 'common.edit'
	| 'common.delete'
	| 'common.confirmDelete'
	| 'agents.title'
	| 'agents.description'
	| 'agents.column.endpoint'
	| 'agents.column.response'
	| 'agents.field.code'
	| 'agents.field.name'
	| 'agents.field.description'
	| 'agents.field.systemPrompt'
	| 'agents.field.model'
	| 'agents.field.endpoint'
	| 'agents.field.tools'
	| 'agents.field.skills'
	| 'agents.field.responseFormat'
	| 'agents.field.responseSchema'
	| 'agents.field.enabled'
	| 'agents.responseFormat.text'
	| 'agents.responseFormat.json'
	| 'agents.responseSchema.hint'
	| 'agents.sort.default'
	| 'agents.sort.name'
	| 'agents.sort.code'
	| 'agents.sort.created'
	| 'llm.title'
	| 'llm.description'
	| 'llm.column.baseUrl'
	| 'llm.column.priority'
	| 'llm.column.boundAgents'
	| 'llm.field.code'
	| 'llm.field.name'
	| 'llm.field.baseUrl'
	| 'llm.field.model'
	| 'llm.field.apiKey'
	| 'llm.field.priority'
	| 'llm.field.enabled'
	| 'llm.apiKey.hint'
	| 'llm.sort.default'
	| 'llm.sort.name'
	| 'llm.sort.code'
	| 'llm.sort.priority'
	| 'llm.sort.created'
	| 'common.archive'
	| 'common.refresh'
	| 'common.usedBy'
	| 'common.status'
	| 'skills.title'
	| 'skills.description'
	| 'skills.column.status'
	| 'skills.field.code'
	| 'skills.field.description'
	| 'skills.field.allowedTools'
	| 'skills.field.body'
	| 'skills.field.status'
	| 'skills.field.enabled'
	| 'skills.status.published'
	| 'skills.status.draft'
	| 'skills.sort.default'
	| 'skills.sort.name'
	| 'skills.sort.code'
	| 'skills.sort.created'
	| 'mcp.title'
	| 'mcp.description'
	| 'mcp.column.transport'
	| 'mcp.column.tools'
	| 'mcp.field.code'
	| 'mcp.field.name'
	| 'mcp.field.transport'
	| 'mcp.field.url'
	| 'mcp.field.command'
	| 'mcp.field.timeout'
	| 'mcp.field.enabled'
	| 'mcp.transport.stdio'
	| 'mcp.transport.http'
	| 'mcp.sort.default'
	| 'mcp.sort.name'
	| 'mcp.sort.code'
	| 'mcp.sort.created'
	| 'workflows.title'
	| 'workflows.description'
	| 'workflows.column.nodes'
	| 'workflows.field.code'
	| 'workflows.field.name'
	| 'workflows.field.description'
	| 'workflows.field.agent'
	| 'workflows.field.enabled'
	| 'workflows.sort.default'
	| 'workflows.sort.name'
	| 'workflows.sort.code';

const EN: Record<MessageKey, string> = {
	'app.title': 'web_agent',
	'nav.group.overview': 'Overview',
	'nav.group.access': 'Access control',
	'nav.group.resources': 'Resources',
	'nav.group.security': 'Security',
	'nav.group.settings': 'Settings',
	'nav.group.operations': 'Operations',
	'nav.overview': 'Overview',
	'nav.access.pages': 'Page permissions',
	'nav.access.components': 'Component permissions',
	'nav.access.roles': 'Roles & permissions',
	'nav.access.mappings': 'OAuth role mappings',
	'nav.workflows': 'Workflows',
	'nav.agents': 'Agents',
	'nav.skills': 'Skills',
	'nav.mcp': 'MCP servers',
	'nav.llm': 'LLM endpoints',
	'nav.oauth': 'OAuth identity providers',
	'nav.platformAdmins': 'Platform super admins',
	'nav.preferences': 'Preferences',
	'nav.audit': 'Tool audit',
	'shell.language': 'Language',
	'shell.signOut': 'Sign out',
	'dashboard.title': 'Overview',
	'dashboard.platformOverview': 'Platform overview',
	'dashboard.conversations': 'Conversations',
	'dashboard.runs': 'Runs',
	'dashboard.activeAdmins': 'Active OAuth admins',
	'dashboard.platformAdmins': 'Platform admins',
	'dashboard.quickActions': 'Quick actions',
	'dashboard.editPagePermissions': 'Edit page permissions',
	'dashboard.editComponentPermissions': 'Edit component permissions',
	'dashboard.reviewCasbinRoles': 'Review Casbin roles',
	'dashboard.empty':
		'Your administrator account is active, but all dashboard components are hidden by the current UI policy.',
	'llm.title': 'LLM endpoints',
	'llm.description':
		'OpenAI-compatible endpoints. The first enabled endpoint wins when a request fails over.',
	'llm.column.baseUrl': 'Base URL',
	'llm.column.priority': 'Priority',
	'llm.column.boundAgents': 'Agents',
	'llm.field.code': 'Code',
	'llm.field.name': 'Name',
	'llm.field.baseUrl': 'Base URL',
	'llm.field.model': 'Default model',
	'llm.field.apiKey': 'API key',
	'llm.field.priority': 'Priority',
	'llm.field.enabled': 'Enabled',
	'llm.apiKey.hint': 'Leave blank to keep the stored key.',
	'llm.sort.default': 'Default',
	'llm.sort.name': 'Name',
	'llm.sort.code': 'Code',
	'llm.sort.priority': 'Priority',
	'llm.sort.created': 'Newest',
	'common.archive': 'Archive',
	'common.refresh': 'Refresh tools',
	'common.usedBy': 'Used by',
	'common.status': 'Status',
	'skills.title': 'Skills',
	'skills.description': 'Skill packs contribute instructions only; tool access still comes from the agent\'s tool list.',
	'skills.column.status': 'Status',
	'skills.field.code': 'Code',
	'skills.field.description': 'Description',
	'skills.field.allowedTools': 'Allowed tools',
	'skills.field.body': 'Instruction body',
	'skills.field.status': 'Status',
	'skills.field.enabled': 'Enabled',
	'skills.status.published': 'Published',
	'skills.status.draft': 'Draft',
	'skills.sort.default': 'Default',
	'skills.sort.name': 'Name',
	'skills.sort.code': 'Code',
	'skills.sort.created': 'Newest',
	'mcp.title': 'MCP servers',
	'mcp.description': 'Register MCP servers and refresh their tool catalog.',
	'mcp.column.transport': 'Transport',
	'mcp.column.tools': 'Tools',
	'mcp.field.code': 'Code',
	'mcp.field.name': 'Name',
	'mcp.field.transport': 'Transport',
	'mcp.field.url': 'URL',
	'mcp.field.command': 'Command',
	'mcp.field.timeout': 'Timeout (seconds)',
	'mcp.field.enabled': 'Enabled',
	'mcp.transport.stdio': 'stdio',
	'mcp.transport.http': 'HTTP',
	'mcp.sort.default': 'Default',
	'mcp.sort.name': 'Name',
	'mcp.sort.code': 'Code',
	'mcp.sort.created': 'Newest',
	'workflows.title': 'Workflows',
	'workflows.description': 'Workflow graphs are edited on the canvas; this page manages their metadata.',
	'workflows.column.nodes': 'Nodes',
	'workflows.field.code': 'Code',
	'workflows.field.name': 'Name',
	'workflows.field.description': 'Description',
	'workflows.field.agent': 'Initial agent',
	'workflows.field.enabled': 'Enabled',
	'workflows.sort.default': 'Default',
	'workflows.sort.name': 'Name',
	'workflows.sort.code': 'Code',	'common.search': 'Search',
	'common.sort': 'Sort by',
	'common.apply': 'Apply',
	'common.prev': 'Previous',
	'common.next': 'Next',
	'common.total': '{count} items',
	'common.actions': 'Actions',
	'common.enabled': 'Enabled',
	'common.disabled': 'Disabled',
	'common.code': 'Code',
	'common.name': 'Name',
	'common.model': 'Model',
	'common.created': 'Created',
	'common.empty': 'Nothing configured yet.',
	'common.save': 'Save changes',
	'common.cancel': 'Cancel',
	'common.create': 'Create',
	'common.edit': 'Edit',
	'common.delete': 'Delete',
	'common.confirmDelete': 'Delete this item?',
	'agents.title': 'Agents',
	'agents.description':
		'Each agent picks a configured LLM connection and binds skills and tools explicitly.',
	'agents.column.endpoint': 'LLM connection',
	'agents.column.response': 'Output',
	'agents.field.code': 'Code',
	'agents.field.name': 'Name',
	'agents.field.description': 'Description',
	'agents.field.systemPrompt': 'System prompt',
	'agents.field.model': 'Model',
	'agents.field.endpoint': 'LLM connection',
	'agents.field.tools': 'Tools',
	'agents.field.skills': 'Skills',
	'agents.field.responseFormat': 'Output format',
	'agents.field.responseSchema': 'JSON schema',
	'agents.field.enabled': 'Enabled',
	'agents.responseFormat.text': 'Plain text',
	'agents.responseFormat.json': 'JSON',
	'agents.responseSchema.hint': 'Leave blank for any JSON value.',
	'agents.sort.default': 'Default',
	'agents.sort.name': 'Name',
	'agents.sort.code': 'Code',
	'agents.sort.created': 'Newest'
};

const ZH_CN: Record<MessageKey, string> = {
	'app.title': 'web_agent',
	'nav.group.overview': '概览',
	'nav.group.access': '访问控制',
	'nav.group.resources': '资源',
	'nav.group.security': '安全',
	'nav.group.settings': '设置',
	'nav.group.operations': '运维',
	'nav.overview': '概览',
	'nav.access.pages': '页面权限',
	'nav.access.components': '组件权限',
	'nav.access.roles': '角色与权限',
	'nav.access.mappings': 'OAuth 角色映射',
	'nav.workflows': '工作流',
	'nav.agents': '智能体',
	'nav.skills': '技能',
	'nav.mcp': 'MCP 服务器',
	'nav.llm': 'LLM 端点',
	'nav.oauth': 'OAuth 身份提供方',
	'nav.platformAdmins': '平台超级管理员',
	'nav.preferences': '偏好设置',
	'nav.audit': '工具审计',
	'shell.language': '语言',
	'shell.signOut': '退出登录',
	'dashboard.title': '概览',
	'dashboard.platformOverview': '平台概览',
	'dashboard.conversations': '会话',
	'dashboard.runs': '运行',
	'dashboard.activeAdmins': '活跃 OAuth 管理员',
	'dashboard.platformAdmins': '平台管理员',
	'dashboard.quickActions': '快捷操作',
	'dashboard.editPagePermissions': '编辑页面权限',
	'dashboard.editComponentPermissions': '编辑组件权限',
	'dashboard.reviewCasbinRoles': '查看 Casbin 角色',
	'dashboard.empty': '管理员账号有效，但当前界面策略隐藏了全部概览组件。',
	'llm.title': 'LLM 端点',
	'llm.description': 'OpenAI 兼容接口配置。请求失败时按优先级切到下一个启用的连接。',
	'llm.column.baseUrl': '接口地址',
	'llm.column.priority': '优先级',
	'llm.column.boundAgents': '引用它的智能体',
	'llm.field.code': '编码',
	'llm.field.name': '名称',
	'llm.field.baseUrl': '接口地址',
	'llm.field.model': '默认模型',
	'llm.field.apiKey': 'API Key',
	'llm.field.priority': '优先级',
	'llm.field.enabled': '启用',
	'llm.apiKey.hint': '留空表示沿用已存的 Key。',
	'llm.sort.default': '默认',
	'llm.sort.name': '名称',
	'llm.sort.code': '编码',
	'llm.sort.priority': '优先级',
	'llm.sort.created': '最新',
	'common.archive': '归档',
	'common.refresh': '刷新工具目录',
	'common.usedBy': '被引用',
	'common.status': '状态',
	'skills.title': '技能',
	'skills.description': '技能只贡献指令正文；工具权限仍然只看智能体勾选的工具清单。',
	'skills.column.status': '状态',
	'skills.field.code': '编码',
	'skills.field.description': '描述',
	'skills.field.allowedTools': '允许的工具',
	'skills.field.body': '指令正文',
	'skills.field.status': '状态',
	'skills.field.enabled': '启用',
	'skills.status.published': '已发布',
	'skills.status.draft': '草稿',
	'skills.sort.default': '默认',
	'skills.sort.name': '名称',
	'skills.sort.code': '编码',
	'skills.sort.created': '最新',
	'mcp.title': 'MCP 服务器',
	'mcp.description': '注册 MCP 服务器并刷新它的工具目录。',
	'mcp.column.transport': '传输方式',
	'mcp.column.tools': '工具',
	'mcp.field.code': '编码',
	'mcp.field.name': '名称',
	'mcp.field.transport': '传输方式',
	'mcp.field.url': '地址',
	'mcp.field.command': '命令',
	'mcp.field.timeout': '超时（秒）',
	'mcp.field.enabled': '启用',
	'mcp.transport.stdio': 'stdio',
	'mcp.transport.http': 'HTTP',
	'mcp.sort.default': '默认',
	'mcp.sort.name': '名称',
	'mcp.sort.code': '编码',
	'mcp.sort.created': '最新',
	'workflows.title': '工作流',
	'workflows.description': '工作流图在画布上编排；这一页只管理它的元信息。',
	'workflows.column.nodes': '节点数',
	'workflows.field.code': '编码',
	'workflows.field.name': '名称',
	'workflows.field.description': '描述',
	'workflows.field.agent': '初始智能体',
	'workflows.field.enabled': '启用',
	'workflows.sort.default': '默认',
	'workflows.sort.name': '名称',
	'workflows.sort.code': '编码',	'common.search': '搜索',
	'common.sort': '排序方式',
	'common.apply': '应用',
	'common.prev': '上一页',
	'common.next': '下一页',
	'common.total': '共 {count} 项',
	'common.actions': '操作',
	'common.enabled': '已启用',
	'common.disabled': '已停用',
	'common.code': '编码',
	'common.name': '名称',
	'common.model': '模型',
	'common.created': '创建时间',
	'common.empty': '尚未配置资源。',
	'common.save': '保存变更',
	'common.cancel': '取消',
	'common.create': '新建',
	'common.edit': '编辑',
	'common.delete': '删除',
	'common.confirmDelete': '确认删除这条记录？',
	'agents.title': '智能体',
	'agents.description': '每个智能体选择已配置的 LLM 连接，并明确绑定技能和工具。',
	'agents.column.endpoint': 'LLM 连接',
	'agents.column.response': '输出',
	'agents.field.code': '编码',
	'agents.field.name': '名称',
	'agents.field.description': '描述',
	'agents.field.systemPrompt': '系统提示词',
	'agents.field.model': '模型',
	'agents.field.endpoint': 'LLM 连接',
	'agents.field.tools': '工具',
	'agents.field.skills': '技能',
	'agents.field.responseFormat': '输出格式',
	'agents.field.responseSchema': 'JSON Schema',
	'agents.field.enabled': '启用',
	'agents.responseFormat.text': '纯文本',
	'agents.responseFormat.json': 'JSON',
	'agents.responseSchema.hint': '留空表示只要求是 JSON，不限定结构。',
	'agents.sort.default': '默认',
	'agents.sort.name': '名称',
	'agents.sort.code': '编码',
	'agents.sort.created': '最新'
};

/** 各语言的词典。 */
export const DICTIONARIES: Record<Locale, Record<MessageKey, string>> = {
	en: EN,
	'zh-CN': ZH_CN
};

/** 把任意输入归一成受支持的语言。 */
export function normalizeLocale(raw: string | null | undefined): Locale {
	const text = (raw ?? '').trim();
	if (text.toLowerCase().startsWith('zh')) return 'zh-CN';
	return DEFAULT_LOCALE;
}

/**
 * 取一条文案。
 *
 * 支持 `{name}` 占位符：`t('zh-CN', 'common.total', { count: 3 })` 得到
 * 「共 3 项」。没给对应参数时占位符原样保留——这比静默替换成空串更容易
 * 被发现。
 */
export function t(
	locale: Locale,
	key: MessageKey,
	params?: Record<string, string | number>
): string {
	const raw = DICTIONARIES[locale][key] ?? DICTIONARIES[DEFAULT_LOCALE][key] ?? key;
	if (!params) return raw;
	return raw.replace(/\{(\w+)\}/g, (match, name: string) =>
		name in params ? String(params[name]) : match
	);
}

/** 页面 code 到导航文案的映射（导航标题来自服务端注册表，这里只做兜底翻译）。 */
const PAGE_KEYS: Record<string, MessageKey> = {
	dashboard: 'nav.overview',
	'access_pages': 'nav.access.pages',
	'access_components': 'nav.access.components',
	'access_roles': 'nav.access.roles',
	'access_mappings': 'nav.access.mappings',
	workflows: 'nav.workflows',
	agents: 'nav.agents',
	skills: 'nav.skills',
	mcp: 'nav.mcp',
	llm: 'nav.llm',
	oauth_providers: 'nav.oauth',
	platform_admins: 'nav.platformAdmins',
	preferences: 'nav.preferences',
	audit: 'nav.audit'
};

/** 导航分组到分组标题的映射。 */
const GROUP_KEYS: Record<string, MessageKey> = {
	Overview: 'nav.group.overview',
	'Access control': 'nav.group.access',
	Resources: 'nav.group.resources',
	Security: 'nav.group.security',
	Settings: 'nav.group.settings',
	Operations: 'nav.group.operations'
};

/** 取页面导航标题；没有登记时退回服务端给的标题。 */
export function pageTitle(locale: Locale, code: string, fallback: string): string {
	const key = PAGE_KEYS[code];
	return key ? t(locale, key) : fallback;
}

/** 取导航分组标题。 */
export function groupTitle(locale: Locale, group: string, fallback: string): string {
	const key = GROUP_KEYS[group];
	return key ? t(locale, key) : fallback;
}