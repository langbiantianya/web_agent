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
/** 技能。 */
export interface AdminSkill {
	code: string;
	name: string;
	description: string;
	allowedTools: string[];
	body: string;
	status: 'published' | 'draft';
	enabled: boolean;
	origin: string;
	version: number;
	contentHash: string;
	usedBy: string[];
	createdAt: string | null;
	updatedAt: string | null;
}

/** 技能列表响应。 */
export interface SkillListPayload {
	items: AdminSkill[];
	total: number;
	page: ListPage;
}

/** 写入技能时提交的字段。 */
export interface SkillInput {
	code?: string;
	description: string;
	allowedTools: string[];
	body: string;
	status: 'published' | 'draft';
	enabled: boolean;
}

/** MCP 服务器。 */
export interface McpServer {
	code: string;
	name: string;
	transport: 'stdio' | 'http';
	command: string;
	url: string;
	envKeys: string[];
	headerKeys: string[];
	timeoutSec: number;
	enabled: boolean;
	toolCount: number;
	usedBy: string[];
	createdAt: string | null;
	updatedAt: string | null;
}

/** MCP 服务器列表响应。 */
export interface McpListPayload {
	items: McpServer[];
	total: number;
	page: ListPage;
}

/** 写入 MCP 服务器时提交的字段。 */
export interface McpInput {
	code?: string;
	name: string;
	transport: 'stdio' | 'http';
	url: string;
	command: string;
	timeoutSec: number;
	enabled: boolean;
}

/** 工作流。 */
export interface AdminWorkflow {
	code: string;
	name: string;
	description: string;
	enabled: boolean;
	version: number;
	nodeCount: number;
	createdAt: string | null;
	updatedAt: string | null;
}

/** 工作流列表响应。 */
export interface WorkflowListPayload {
	items: AdminWorkflow[];
	total: number;
	page: ListPage;
	options: { agents: { code: string; name: string }[] };
}

/** 写入工作流时提交的字段。 */
export interface WorkflowInput {
	code?: string;
	name: string;
	description: string;
	agentCode: string;
	enabled: boolean;
}
