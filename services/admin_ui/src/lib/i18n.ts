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
	| 'dashboard.empty';

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
		'Your administrator account is active, but all dashboard components are hidden by the current UI policy.'
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
	'dashboard.empty': '管理员账号有效，但当前界面策略隐藏了全部概览组件。'
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

/** 取一条文案。 */
export function t(locale: Locale, key: MessageKey): string {
	return DICTIONARIES[locale][key] ?? DICTIONARIES[DEFAULT_LOCALE][key] ?? key;
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