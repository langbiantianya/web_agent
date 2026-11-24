import { error, fail, redirect } from '@sveltejs/kit';
import type { ActionFailure } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
	AdminApiError,
	createAgent,
	deleteAgent,
	loadAgents,
	toggleAgent,
	updateAgent
} from '#lib/server/admin-api';
import type { AgentInput } from '#lib/types';

/** 表单里的 JSON Schema 文本。 */
function parseSchema(raw: string): Record<string, unknown> | null {
	const text = raw.trim();
	if (!text) return null;
	const parsed = JSON.parse(text);
	if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
		throw new Error('invalid_response_schema');
	}
	return parsed as Record<string, unknown>;
}

/** 把表单读成 Agent 写入用的形状。 */
function readInput(form: FormData): AgentInput {
	return {
		name: String(form.get('name') ?? ''),
		description: String(form.get('description') ?? ''),
		systemPrompt: String(form.get('systemPrompt') ?? ''),
		model: String(form.get('model') ?? ''),
		llmEndpointCode: String(form.get('llmEndpointCode') ?? ''),
		tools: form.getAll('tools').map(String),
		skills: form.getAll('skills').map(String),
		responseFormat: form.get('responseFormat') === 'json' ? 'json' : 'text',
		responseSchema: parseSchema(String(form.get('responseSchema') ?? '')),
		enabled: form.get('enabled') === 'on'
	};
}

/** 表单错误载荷的形状。 */
export interface AgentFormErrors {
	message: string;
	fieldErrors: Record<string, string>;
}

/** 把管理端 API 的失败翻译成表单错误。 */
function toFailure(cause: unknown): ActionFailure<AgentFormErrors> {
	if (cause instanceof AdminApiError) {
		const fieldErrors = (
			cause as AdminApiError & { fieldErrors?: Record<string, string> }
		).fieldErrors;
		return fail(cause.status === 404 ? 404 : 400, {
			message: cause.message,
			fieldErrors: fieldErrors ?? {}
		});
	}
	throw cause;
}

export const load: PageServerLoad = async ({ locals, request, url }) => {
	const session = locals.session;
	if (!session) error(401, '请先登录管理后台');
	const cookie = request.headers.get('cookie') ?? '';
	try {
		const payload = await loadAgents(cookie, {
			q: url.searchParams.get('q') ?? '',
			sort: url.searchParams.get('sort') ?? '',
			page: Number(url.searchParams.get('page') ?? '1') || 1,
			size: Number(url.searchParams.get('size') ?? '20') || 20
		});
		return { payload, editing: url.searchParams.get('edit') ?? '' };
	} catch (cause) {
		if (cause instanceof AdminApiError) error(cause.status, cause.message);
		throw cause;
	}
};

export const actions: Actions = {
	create: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '').trim();
		try {
			const input = { ...readInput(form), code };
			await createAgent(request.headers.get('cookie') ?? '', session.csrfToken, input);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/agents');
	},

	update: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '').trim();
		try {
			await updateAgent(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				code,
				readInput(form)
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/agents');
	},

	toggle: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '');
		try {
			await toggleAgent(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				code,
				form.get('enabled') === 'on'
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/agents');
	},

	delete: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '');
		try {
			await deleteAgent(request.headers.get('cookie') ?? '', session.csrfToken, code);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/agents');
	}
};