import { error, fail, redirect } from '@sveltejs/kit';
import type { ActionFailure } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
	AdminApiError,
	createLlmEndpoint,
	deleteLlmEndpoint,
	loadLlmEndpoints,
	toggleLlmEndpoint,
	updateLlmEndpoint
} from '#lib/server/admin-api';
import type { LlmInput } from '#lib/types';

/** 表单错误载荷的形状。 */
export interface LlmFormErrors {
	message: string;
	fieldErrors: Record<string, string>;
}

/** 把表单读成 LLM 端点写入用的形状。 */
function readInput(form: FormData): LlmInput {
	return {
		name: String(form.get('name') ?? ''),
		baseUrl: String(form.get('baseUrl') ?? ''),
		model: String(form.get('model') ?? ''),
		apiKey: String(form.get('apiKey') ?? ''),
		priority: Number(form.get('priority') ?? '100') || 0,
		enabled: form.get('enabled') === 'on'
	};
}

/** 把管理端 API 的失败翻译成表单错误。 */
function toFailure(cause: unknown): ActionFailure<LlmFormErrors> {
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
		const payload = await loadLlmEndpoints(cookie, {
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
			await createLlmEndpoint(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				{ ...readInput(form), code }
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/llm');
	},

	update: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '').trim();
		try {
			await updateLlmEndpoint(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				code,
				readInput(form)
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/llm');
	},

	toggle: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '');
		try {
			await toggleLlmEndpoint(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				code,
				form.get('enabled') === 'on'
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/llm');
	},

	delete: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '');
		try {
			await deleteLlmEndpoint(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				code
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/llm');
	}
};