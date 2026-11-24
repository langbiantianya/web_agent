/** 与 Python 管理面 API（`/admin/api/*`）对齐的类型。 */

/** 管理员主体。 */
export interface AdminPrincipal {
	kind: string;
	principalId: number;
	username: string;
	tenantId: string | null;
	isOwner: boolean;
	isPlatformSuperAdmin: boolean;
}

/** 界面偏好：主题、色温、密度。 */
export interface AdminPreferences {
	theme: string;
	colorMode: string;
	density: string;
}

/** 导航里的一个页面。 */
export interface AdminPage {
	code: string;
	routeKey: string;
	title: string;
	navGroup: string;
	navOrder: number;
}

/** 会话摘要：身份 + 偏好 + 可见页面 + CSRF 令牌。 */
export interface AdminSession {
	principal: AdminPrincipal;
	preferences: AdminPreferences;
	pages: AdminPage[];
	csrfToken: string;
	components?: Record<string, Record<string, boolean>>;
}

/** 概览页指标。 */
export interface DashboardCounts {
	conversations: number;
	runs: number;
	admins: number;
	platformAdmins: number;
}

/** 概览页载荷。 */
export interface DashboardPayload {
	counts: DashboardCounts;
	components: { metrics: boolean; quickActions: boolean };
}

/** Agent 列表项。 */
export interface AdminAgent {
	code: string;
	name: string;
	description: string;
	systemPrompt: string;
	model: string;
	llmEndpointCode: string;
	tools: string[];
	skills: string[];
	responseFormat: 'text' | 'json';
	responseSchema: Record<string, unknown> | null;
	enabled: boolean;
	createdAt: string | null;
	updatedAt: string | null;
}

/** Agent 表单需要的候选项。 */
export interface AgentOptions {
	llmEndpoints: { code: string; name: string; model: string; enabled: boolean }[];
	skills: { code: string; name: string }[];
	mcpServers: { code: string; name: string }[];
	mcpTools: string[];
}

/** 列表分页参数。 */
export interface ListPage {
	q: string;
	sort: string;
	page: number;
	size: number;
}

/** Agent 列表响应。 */
export interface AgentListPayload {
	items: AdminAgent[];
	total: number;
	page: ListPage;
	options: AgentOptions;
}

/** 写入 Agent 时提交的字段。 */
export interface AgentInput {
	code?: string;
	name: string;
	description: string;
	systemPrompt: string;
	model: string;
	llmEndpointCode: string;
	tools: string[];
	skills: string[];
	responseFormat: 'text' | 'json';
	responseSchema: Record<string, unknown> | null;
	enabled: boolean;
}

/** LLM 端点。 */
export interface LlmEndpoint {
	code: string;
	name: string;
	baseUrl: string;
	model: string;
	priority: number;
	enabled: boolean;
	hasApiKey: boolean;
	boundAgents: number;
	createdAt: string | null;
	updatedAt: string | null;
}

/** LLM 端点列表响应。 */
export interface LlmListPayload {
	items: LlmEndpoint[];
	total: number;
	page: ListPage;
}

/** 写入 LLM 端点时提交的字段。 */
export interface LlmInput {
	code?: string;
	name: string;
	baseUrl: string;
	model: string;
	apiKey: string;
	priority: number;
	enabled: boolean;
}