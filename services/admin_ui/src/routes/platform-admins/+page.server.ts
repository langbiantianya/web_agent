	import { error, fail, redirect } from '@sveltejs/kit';
	import type { ActionFailure } from '@sveltejs/kit';
	import type { Actions, PageServerLoad } from './$types';
	import {
		AdminApiError,
		createPlatformAdmin,
		deletePlatformAdmin,
		loadPlatformAdmins,
		resetPlatformAdminPassword,
		undoDelete
	} from '#lib/server/admin-api';

	/** 表单错误载荷的形状。 */
	export interface PlatformAdminFormErrors {
		message: string;
		fieldErrors: Record<string, string>;
	}

	/** 把管理端 API 的失败翻译成表单错误。 */
	function toFailure(cause: unknown): ActionFailure<PlatformAdminFormErrors> {
		if (cause instanceof AdminApiError) {
			return fail(cause.status === 404 ? 404 : 400, {
				message: cause.message,
				fieldErrors: cause.fieldErrors
			});
		}
		throw cause;
	}

	export const load: PageServerLoad = async ({ locals, request, url }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		try {
			const payload = await loadPlatformAdmins(request.headers.get('cookie') ?? '', {
				q: url.searchParams.get('q') ?? '',
				page: Number(url.searchParams.get('page') ?? '1') || 1,
				size: Number(url.searchParams.get('size') ?? '20') || 20
			});
			return { payload, undoId: url.searchParams.get('undo') ?? '' };
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
			const username = String(form.get('username') ?? '').trim();
			const email = String(form.get('email') ?? '').trim();
			const phone = String(form.get('phone') ?? '').trim();
			try {
				await createPlatformAdmin(request.headers.get('cookie') ?? '', session.csrfToken, {
					username,
					email,
					phone
				});
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/platform-admins');
		},

		delete: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const accountId = Number(form.get('accountId') ?? '0');
			let undoId = '';
			try {
				const created = await deletePlatformAdmin(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					accountId
				);
				undoId = created === null ? '' : String(created);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, `/admin/platform-admins${undoId ? `?undo=${undoId}` : ''}`);
		},

		undo: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const undoId = Number(form.get('undoId') ?? '0');
			try {
				await undoDelete(request.headers.get('cookie') ?? '', session.csrfToken, undoId);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/platform-admins');
		},

		reset: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const accountId = Number(form.get('accountId') ?? '0');
			try {
				await resetPlatformAdminPassword(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					accountId
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/platform-admins');
		}
	};
