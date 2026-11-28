/**
 * 管理面 API 客户端（服务端专用）。
 *
 * **所有请求都必须经过这里**：浏览器永远不直接访问 Python 管理端。
 * 原因有三个——
 *
 * 1. **Cookie 不出服务端**：会话 Cookie 只在 Node 与 Python 之间流转，
 *    浏览器拿不到会话令牌，也就无法把它塞进第三方请求。
 * 2. **CSRF 不由浏览器拼**：CSRF 令牌由 Python 侧随会话摘要一起下发，
 *    写操作时由本模块放进 `X-CSRF-Token` 头，浏览器全程不参与。
 * 3. **越权判断只有一处**：Casbin 判定始终在 Python 侧，前端拿到的
 *    「可见页面/组件」只是渲染依据，不是授权依据。
 */

import type {
	AccessComponentItem,
	AccessComponentsPayload,
	AccessPageItem,
	AccessPagesPayload,
	AdminAgent,
	AdminSession,
	AdminSkill,
	AdminWorkflow,
	AgentInput,
	AgentListPayload,
	AuditItem,
	AuditListPayload,
	DashboardPayload,
	LlmEndpoint,
	LlmInput,
	LlmListPayload,
	MappingItem,
	MappingListPayload,
	McpInput,
	McpListPayload,
	McpServer,
	PlatformAdminItem,
	PlatformAdminListPayload,
	PreferencesPayload,
	RoleItem,
	RoleListPayload,
	SkillInput,
	SkillListPayload,
	WorkflowInput,
	WorkflowListPayload
} from '#lib/types';
import { adminApiBaseUrl } from '#lib/server/config';



/** 管理端调用失败。 */
export class AdminApiError extends Error {
	/** HTTP 状态码。 */
	readonly status: number;

	/** 稳定的机器码（``unauthorized`` / ``not_found`` / ``duplicate`` …）。 */
	readonly code: string;

	/** 字段级错误：字段名 → 可直接展示的说明。 */
	readonly fieldErrors: Record<string, string>;

	/**
	 * 构造错误。
	 *
	 * @param status HTTP 状态码。
	 * @param message 可直接展示给管理员的错误说明。
	 * @param code 机器码；缺省时按状态码推断一个粗粒度的码。
	 * @param fieldErrors 字段级错误。
	 */
	constructor(
		status: number,
		message: string,
		code = '',
		fieldErrors: Record<string, string> = {}
	) {
		super(message);
		this.name = 'AdminApiError';
		this.status = status;
		this.code = code || inferCode(status);
		this.fieldErrors = fieldErrors;
	}
}

/** 按状态码推断一个粗粒度机器码。 */
function inferCode(status: number): string {
	if (status === 401) return 'unauthorized';
	if (status === 403) return 'forbidden';
	if (status === 404) return 'not_found';
	if (status === 409) return 'conflict';
	return 'error';
}

/** 一次转发的输入。 */
export interface AdminFetchOptions {
	/** 请求方法。 */
	method?: string;
	/** 请求体。 */
	body?: string;
	/** 附加请求头。 */
	headers?: Record<string, string>;
	/** 浏览器带来的 Cookie 串。 */
	cookie?: string;
	/** CSRF 令牌（写操作必填）。 */
	csrfToken?: string;
}

/** 调用 Python 管理面 API。 */
export async function adminFetch<T>(
	path: string,
	options: AdminFetchOptions = {}
): Promise<T> {
	const headers = new Headers(options.headers ?? {});
	if (options.cookie) headers.set('cookie', options.cookie);
	if (options.csrfToken) headers.set('x-csrf-token', options.csrfToken);
	if (options.body !== undefined && !headers.has('content-type')) {
		headers.set('content-type', 'application/json');
	}
	const response = await fetch(`${adminApiBaseUrl()}${path}`, {
		method: options.method ?? 'GET',
		headers,
		body: options.body
	});
	if (!response.ok) {
		let message = `管理端接口返回 ${response.status}`;
		let code = '';
		let fieldErrors: Record<string, string> = {};
		try {
			const payload = (await response.json()) as {
				message?: string;
				code?: string;
				fieldErrors?: Record<string, string>;
			};
			if (payload?.message) message = payload.message;
			if (payload?.code) code = payload.code;
			if (payload?.fieldErrors) fieldErrors = payload.fieldErrors;
		} catch {
			// 响应体不是 JSON：用状态码兜底，不把原文透给前端。
		}
		throw new AdminApiError(response.status, message, code, fieldErrors);
	}
	return (await response.json()) as T;
}

/** 读取当前会话摘要；未登录时返回 null。 */
export async function loadSession(cookie: string): Promise<AdminSession | null> {
	try {
		return await adminFetch<AdminSession>('/admin/api/session', { cookie });
	} catch (error) {
		if (error instanceof AdminApiError && error.status === 401) return null;
		throw error;
	}
}

/** 读取概览页载荷。 */
export async function loadDashboard(cookie: string, csrfToken: string): Promise<DashboardPayload> {
	return await adminFetch<DashboardPayload>('/admin/api/dashboard', { cookie });
}

/** 读取 Agent 列表（含表单候选项）。 */
export async function loadAgents(
	cookie: string,
	query: { q?: string; sort?: string; page?: number; size?: number } = {}
): Promise<AgentListPayload> {
	const params = new URLSearchParams();
	if (query.q) params.set('q', query.q);
	if (query.sort) params.set('sort', query.sort);
	if (query.page) params.set('page', String(query.page));
	if (query.size) params.set('size', String(query.size));
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return await adminFetch<AgentListPayload>(`/admin/api/agents${suffix}`, { cookie });
}

/** 创建 Agent。 */
export async function createAgent(
	cookie: string,
	csrfToken: string,
	input: AgentInput
): Promise<AdminAgent> {
	const result = await adminFetch<{ item: AdminAgent }>('/admin/api/agents', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 更新 Agent。 */
export async function updateAgent(
	cookie: string,
	csrfToken: string,
	code: string,
	input: AgentInput
): Promise<AdminAgent> {
	const result = await adminFetch<{ item: AdminAgent }>(
		`/admin/api/agents/${encodeURIComponent(code)}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 切换 Agent 的启用状态。 */
export async function toggleAgent(
	cookie: string,
	csrfToken: string,
	code: string,
	enabled: boolean
): Promise<AdminAgent> {
	const result = await adminFetch<{ item: AdminAgent }>(
		`/admin/api/agents/${encodeURIComponent(code)}/toggle`,
		{
			method: 'POST',
			cookie,
			csrfToken,
			body: JSON.stringify({ enabled })
		}
	);
	return result.item;
}

/** 删除 Agent。 */
export async function deleteAgent(
	cookie: string,
	csrfToken: string,
	code: string
): Promise<number | null> {
	const result = await adminFetch<{ ok: boolean; undoId?: number }>(`/admin/api/agents/${encodeURIComponent(code)}`, {
		method: 'DELETE',
		cookie,
		csrfToken
	});
	return result.undoId ?? null;
}

/** 读取 LLM 端点列表。 */
export async function loadLlmEndpoints(
	cookie: string,
	query: { q?: string; sort?: string; page?: number; size?: number } = {}
): Promise<LlmListPayload> {
	const params = new URLSearchParams();
	if (query.q) params.set('q', query.q);
	if (query.sort) params.set('sort', query.sort);
	if (query.page) params.set('page', String(query.page));
	if (query.size) params.set('size', String(query.size));
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return await adminFetch<LlmListPayload>(`/admin/api/llm${suffix}`, { cookie });
}

/** 新增 LLM 端点。 */
export async function createLlmEndpoint(
	cookie: string,
	csrfToken: string,
	input: LlmInput
): Promise<LlmEndpoint> {
	const result = await adminFetch<{ item: LlmEndpoint }>('/admin/api/llm', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 更新 LLM 端点。 */
export async function updateLlmEndpoint(
	cookie: string,
	csrfToken: string,
	code: string,
	input: LlmInput
): Promise<LlmEndpoint> {
	const result = await adminFetch<{ item: LlmEndpoint }>(
		`/admin/api/llm/${encodeURIComponent(code)}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 切换 LLM 端点的启用状态。 */
export async function toggleLlmEndpoint(
	cookie: string,
	csrfToken: string,
	code: string,
	enabled: boolean
): Promise<LlmEndpoint> {
	const result = await adminFetch<{ item: LlmEndpoint }>(
		`/admin/api/llm/${encodeURIComponent(code)}/toggle`,
		{
			method: 'POST',
			cookie,
			csrfToken,
			body: JSON.stringify({ enabled })
		}
	);
	return result.item;
}

/** 删除 LLM 端点。 */
export async function deleteLlmEndpoint(
	cookie: string,
	csrfToken: string,
	code: string
): Promise<number | null> {
	const result = await adminFetch<{ ok: boolean; undoId?: number }>(`/admin/api/llm/${encodeURIComponent(code)}`, {
		method: 'DELETE',
		cookie,
		csrfToken
	});
	return result.undoId ?? null;
}
/** 把查询参数拼成列表接口的查询串。 */
function listQuery(
	query: { q?: string; sort?: string; page?: number; size?: number } = {}
): string {
	const params = new URLSearchParams();
	if (query.q) params.set('q', query.q);
	if (query.sort) params.set('sort', query.sort);
	if (query.page) params.set('page', String(query.page));
	if (query.size) params.set('size', String(query.size));
	return params.toString() ? `?${params.toString()}` : '';
}

/** 读取技能列表。 */
export async function loadSkills(
	cookie: string,
	query: { q?: string; sort?: string; page?: number; size?: number } = {}
): Promise<SkillListPayload> {
	return await adminFetch<SkillListPayload>(`/admin/api/skills${listQuery(query)}`, { cookie });
}

/** 创建技能。 */
export async function createSkill(
	cookie: string,
	csrfToken: string,
	input: SkillInput
): Promise<AdminSkill> {
	const result = await adminFetch<{ item: AdminSkill }>('/admin/api/skills', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 更新技能。 */
export async function updateSkill(
	cookie: string,
	csrfToken: string,
	code: string,
	input: SkillInput
): Promise<AdminSkill> {
	const result = await adminFetch<{ item: AdminSkill }>(
		`/admin/api/skills/${encodeURIComponent(code)}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 归档技能。 */
export async function archiveSkill(
	cookie: string,
	csrfToken: string,
	code: string
): Promise<void> {
	await adminFetch<{ ok: boolean }>(`/admin/api/skills/${encodeURIComponent(code)}/archive`, {
		method: 'POST',
		cookie,
		csrfToken
	});
}

/** 读取 MCP 服务器列表。 */
export async function loadMcpServers(
	cookie: string,
	query: { q?: string; sort?: string; page?: number; size?: number } = {}
): Promise<McpListPayload> {
	return await adminFetch<McpListPayload>(`/admin/api/mcp${listQuery(query)}`, { cookie });
}

/** 注册 MCP 服务器。 */
export async function createMcpServer(
	cookie: string,
	csrfToken: string,
	input: McpInput
): Promise<McpServer> {
	const result = await adminFetch<{ item: McpServer }>('/admin/api/mcp', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 更新 MCP 服务器。 */
export async function updateMcpServer(
	cookie: string,
	csrfToken: string,
	code: string,
	input: McpInput
): Promise<McpServer> {
	const result = await adminFetch<{ item: McpServer }>(
		`/admin/api/mcp/${encodeURIComponent(code)}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 切换 MCP 服务器的启用状态。 */
export async function toggleMcpServer(
	cookie: string,
	csrfToken: string,
	code: string,
	enabled: boolean
): Promise<McpServer> {
	const result = await adminFetch<{ item: McpServer }>(
		`/admin/api/mcp/${encodeURIComponent(code)}/toggle`,
		{
			method: 'POST',
			cookie,
			csrfToken,
			body: JSON.stringify({ enabled })
		}
	);
	return result.item;
}

/** 刷新 MCP 工具目录。 */
export async function refreshMcpCatalog(
	cookie: string,
	csrfToken: string,
	code: string
): Promise<string[]> {
	const result = await adminFetch<{ ok: boolean; imported: string[] }>(
		`/admin/api/mcp/${encodeURIComponent(code)}/refresh`,
		{ method: 'POST', cookie, csrfToken }
	);
	return result.imported;
}

/** 删除 MCP 服务器。 */
export async function deleteMcpServer(
	cookie: string,
	csrfToken: string,
	code: string
): Promise<number | null> {
	const result = await adminFetch<{ ok: boolean; undoId?: number }>(`/admin/api/mcp/${encodeURIComponent(code)}`, {
		method: 'DELETE',
		cookie,
		csrfToken
	});
	return result.undoId ?? null;
}

/** 读取工作流列表。 */
export async function loadWorkflows(
	cookie: string,
	query: { q?: string; sort?: string; page?: number; size?: number } = {}
): Promise<WorkflowListPayload> {
	return await adminFetch<WorkflowListPayload>(`/admin/api/workflows${listQuery(query)}`, {
		cookie
	});
}

/** 新建工作流。 */
export async function createWorkflow(
	cookie: string,
	csrfToken: string,
	input: WorkflowInput
): Promise<AdminWorkflow> {
	const result = await adminFetch<{ item: AdminWorkflow }>('/admin/api/workflows', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 更新工作流元信息。 */
export async function updateWorkflow(
	cookie: string,
	csrfToken: string,
	code: string,
	input: WorkflowInput
): Promise<AdminWorkflow> {
	const result = await adminFetch<{ item: AdminWorkflow }>(
		`/admin/api/workflows/${encodeURIComponent(code)}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 切换工作流的启用状态。 */
export async function toggleWorkflow(
	cookie: string,
	csrfToken: string,
	code: string,
	enabled: boolean
): Promise<AdminWorkflow> {
	const result = await adminFetch<{ item: AdminWorkflow }>(
		`/admin/api/workflows/${encodeURIComponent(code)}/toggle`,
		{
			method: 'POST',
			cookie,
			csrfToken,
			body: JSON.stringify({ enabled })
		}
	);
	return result.item;
}

/** 删除工作流。 */
export async function deleteWorkflow(
	cookie: string,
	csrfToken: string,
	code: string
): Promise<number | null> {
	const result = await adminFetch<{ ok: boolean; undoId?: number }>(`/admin/api/workflows/${encodeURIComponent(code)}`, {
		method: 'DELETE',
		cookie,
		csrfToken
	});
	return result.undoId ?? null;
}


/* ------------------------------------------------------------------ */
/* 访问控制：页面权限 / 组件权限 / 角色 / OAuth 映射                        */
/* ------------------------------------------------------------------ */

/** 读取页面权限矩阵。 */
export async function loadAccessPages(cookie: string): Promise<AccessPagesPayload> {
	return await adminFetch<AccessPagesPayload>('/admin/api/access/pages', { cookie });
}

/** 保存某个租户范围下的页面权限绑定。 */
export async function saveAccessPage(
	cookie: string,
	csrfToken: string,
	pageCode: string,
	input: { tenantId: string | null; permissions: string[]; operation?: string }
): Promise<void> {
	await adminFetch(`/admin/api/access/pages/${encodeURIComponent(pageCode)}`, {
		method: 'PUT',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
}

/** 读取组件权限矩阵。 */
export async function loadAccessComponents(
	cookie: string
): Promise<AccessComponentsPayload> {
	return await adminFetch<AccessComponentsPayload>('/admin/api/access/components', { cookie });
}

/** 保存某个租户范围下的组件权限绑定。 */
export async function saveAccessComponent(
	cookie: string,
	csrfToken: string,
	pageCode: string,
	componentCode: string,
	input: { tenantId: string | null; permissions: string[]; operation?: string }
): Promise<void> {
	await adminFetch(
		`/admin/api/access/components/${encodeURIComponent(pageCode)}/${encodeURIComponent(componentCode)}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
}

/** 读取角色列表。 */
export async function loadRoles(
	cookie: string,
	query: { q?: string; sort?: string; page?: number; size?: number } = {}
): Promise<RoleListPayload> {
	const params = new URLSearchParams();
	if (query.q) params.set('q', query.q);
	if (query.sort) params.set('sort', query.sort);
	if (query.page) params.set('page', String(query.page));
	if (query.size) params.set('size', String(query.size));
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return await adminFetch<RoleListPayload>(`/admin/api/access/roles${suffix}`, { cookie });
}

/** 创建角色。 */
export async function createRole(
	cookie: string,
	csrfToken: string,
	input: { code: string; domain: string; permissions: string[] }
): Promise<RoleItem> {
	const result = await adminFetch<{ item: RoleItem }>('/admin/api/access/roles', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 替换角色的权限集合。 */
export async function updateRole(
	cookie: string,
	csrfToken: string,
	roleCode: string,
	input: { domain: string; permissions: string[] }
): Promise<RoleItem> {
	const result = await adminFetch<{ item: RoleItem }>(
		`/admin/api/access/roles/${encodeURIComponent(roleCode)}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 替换角色的父角色。 */
export async function updateRoleInheritance(
	cookie: string,
	csrfToken: string,
	roleCode: string,
	input: { domain: string; parents: string[] }
): Promise<RoleItem> {
	const result = await adminFetch<{ item: RoleItem }>(
		`/admin/api/access/roles/${encodeURIComponent(roleCode)}/inheritance`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 删除角色。 */
export async function deleteRole(
	cookie: string,
	csrfToken: string,
	roleCode: string,
	domain: string
): Promise<void> {
	await adminFetch(
		`/admin/api/access/roles/${encodeURIComponent(roleCode)}?domain=${encodeURIComponent(domain)}`,
		{ method: 'DELETE', cookie, csrfToken }
	);
}

/** 读取 OAuth 角色映射。 */
export async function loadMappings(cookie: string): Promise<MappingListPayload> {
	return await adminFetch<MappingListPayload>('/admin/api/access/mappings', { cookie });
}

/** 新增 OAuth 角色映射。 */
export async function createMapping(
	cookie: string,
	csrfToken: string,
	input: {
		providerId: number;
		claimName: string;
		externalValue: string;
		roleCode: string;
	}
): Promise<MappingItem> {
	const result = await adminFetch<{ item: MappingItem }>('/admin/api/access/mappings', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 修改 OAuth 角色映射。 */
export async function updateMapping(
	cookie: string,
	csrfToken: string,
	mappingId: number,
	input: { roleCode: string; enabled: boolean }
): Promise<MappingItem> {
	const result = await adminFetch<{ item: MappingItem }>(
		`/admin/api/access/mappings/${mappingId}`,
		{
			method: 'PUT',
			cookie,
			csrfToken,
			body: JSON.stringify(input)
		}
	);
	return result.item;
}

/** 删除 OAuth 角色映射。 */
export async function deleteMapping(
	cookie: string,
	csrfToken: string,
	mappingId: number
): Promise<void> {
	await adminFetch(`/admin/api/access/mappings/${mappingId}`, {
		method: 'DELETE',
		cookie,
		csrfToken
	});
}

/* ------------------------------------------------------------------ */
/* 平台超级管理员、外观偏好、工具审计与撤销删除                             */
/* ------------------------------------------------------------------ */

/** 读取平台超级管理员列表。 */
export async function loadPlatformAdmins(
	cookie: string,
	query: { q?: string; page?: number; size?: number } = {}
): Promise<PlatformAdminListPayload> {
	const params = new URLSearchParams();
	if (query.q) params.set('q', query.q);
	if (query.page) params.set('page', String(query.page));
	if (query.size) params.set('size', String(query.size));
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return await adminFetch<PlatformAdminListPayload>(
		`/admin/api/platform-admins${suffix}`,
		{ cookie }
	);
}

/** 创建平台超级管理员。 */
export async function createPlatformAdmin(
	cookie: string,
	csrfToken: string,
	input: { username: string; email: string; phone: string }
): Promise<PlatformAdminItem> {
	const result = await adminFetch<{ item: PlatformAdminItem }>('/admin/api/platform-admins', {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
	return result.item;
}

/** 删除平台超级管理员，返回可撤销的快照编号。 */
export async function deletePlatformAdmin(
	cookie: string,
	csrfToken: string,
	accountId: number
): Promise<number | null> {
	const result = await adminFetch<{ ok?: boolean; undoId?: number }>(
		`/admin/api/platform-admins/${accountId}`,
		{ method: 'DELETE', cookie, csrfToken }
	);
	return result.undoId ?? null;
}

/** 重置平台超级管理员的密码。 */
export async function resetPlatformAdminPassword(
	cookie: string,
	csrfToken: string,
	accountId: number
): Promise<void> {
	await adminFetch(`/admin/api/platform-admins/${accountId}/reset-password`, {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify({})
	});
}

/** 读取外观偏好。 */
export async function loadPreferences(cookie: string): Promise<PreferencesPayload> {
	return await adminFetch<PreferencesPayload>('/admin/api/preferences', { cookie });
}

/** 保存外观偏好。 */
export async function savePreferences(
	cookie: string,
	csrfToken: string,
	input: { theme: string; colorMode: string; density: string }
): Promise<PreferencesPayload> {
	return await adminFetch<PreferencesPayload>('/admin/api/preferences', {
		method: 'PUT',
		cookie,
		csrfToken,
		body: JSON.stringify(input)
	});
}

/** 查询工具审计记录。 */
export async function loadAudit(
	cookie: string,
	query: {
		runId?: string;
		toolName?: string;
		source?: string;
		ok?: string;
		page?: number;
		size?: number;
	} = {}
): Promise<AuditListPayload> {
	const params = new URLSearchParams();
	if (query.runId) params.set('runId', query.runId);
	if (query.toolName) params.set('toolName', query.toolName);
	if (query.source) params.set('source', query.source);
	if (query.ok) params.set('ok', query.ok);
	if (query.page) params.set('page', String(query.page));
	if (query.size) params.set('size', String(query.size));
	const suffix = params.toString() ? `?${params.toString()}` : '';
	return await adminFetch<AuditListPayload>(`/admin/api/audit${suffix}`, { cookie });
}

/** 撤销一次删除。 */
export async function undoDelete(
	cookie: string,
	csrfToken: string,
	undoId: number
): Promise<void> {
	await adminFetch(`/admin/api/undo/${undoId}`, {
		method: 'POST',
		cookie,
		csrfToken,
		body: JSON.stringify({})
	});
}
