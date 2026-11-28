	import { error, fail, redirect } from '@sveltejs/kit';
	import type { ActionFailure } from '@sveltejs/kit';
	import type { Actions, PageServerLoad } from './$types';
	import {
		AdminApiError,
		createMcpServer,
		deleteMcpServer,
		loadMcpServers,
		refreshMcpCatalog,
		toggleMcpServer,
		updateMcpServer,
		undoDelete
} from '#lib/server/admin-api';
	import type { McpInput } from '#lib/types';

	/** 表单错误载荷的形状。 */
	export interface McpFormErrors {
		message: string;
		fieldErrors: Record<string, string>;
	}

	/** 把表单读成 MCP 服务器写入用的形状。 */
	function readInput(form: FormData): McpInput {
		return {
			name: String(form.get('name') ?? ''),
			transport: form.get('transport') === 'stdio' ? 'stdio' : 'http',
			url: String(form.get('url') ?? ''),
			command: String(form.get('command') ?? ''),
			timeoutSec: Number(form.get('timeoutSec') ?? '30') || 30,
			enabled: form.get('enabled') === 'on'
		};
	}

	/** 把管理端 API 的失败翻译成表单错误。 */
	function toFailure(cause: unknown): ActionFailure<McpFormErrors> {
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
			const payload = await loadMcpServers(cookie, {
				q: url.searchParams.get('q') ?? '',
				sort: url.searchParams.get('sort') ?? '',
				page: Number(url.searchParams.get('page') ?? '1') || 1,
				size: Number(url.searchParams.get('size') ?? '20') || 20
			});
			return {
				payload,
				editing: url.searchParams.get('edit') ?? '',
				undoId: url.searchParams.get('undo') ?? ''
			};
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
				await createMcpServer(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					{ ...readInput(form), code }
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/mcp');
		},

		update: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const code = String(form.get('code') ?? '').trim();
			try {
				await updateMcpServer(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					code,
					readInput(form)
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/mcp');
		},

		toggle: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const code = String(form.get('code') ?? '');
			try {
				await toggleMcpServer(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					code,
					form.get('enabled') === 'on'
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/mcp');
		},

		refresh: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const code = String(form.get('code') ?? '');
			try {
				await refreshMcpCatalog(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					code
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/mcp');
		},

		delete: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const code = String(form.get('code') ?? '');
			let undoId: number | null = null;
			try {
				undoId = await deleteMcpServer(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					code
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/mcp' + (undoId ? `?undo=${undoId}` : ''));
		},

		undo: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const undoId = Number(form.get('undoId') ?? '0');
			try {
				await undoDelete(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					undoId
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/mcp');
		}
	};
