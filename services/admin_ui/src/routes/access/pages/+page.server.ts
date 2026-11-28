	import { error, fail, redirect } from '@sveltejs/kit';
	import type { ActionFailure } from '@sveltejs/kit';
	import type { Actions, PageServerLoad } from './$types';
	import { AdminApiError, loadAccessPages, saveAccessPage } from '#lib/server/admin-api';

	/** 表单错误载荷的形状。 */
	export interface AccessFormErrors {
		message: string;
		fieldErrors: Record<string, string>;
	}

	/** 把管理端 API 的失败翻译成表单错误。 */
	function toFailure(cause: unknown): ActionFailure<AccessFormErrors> {
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
			const payload = await loadAccessPages(request.headers.get('cookie') ?? '');
			return { payload, editing: url.searchParams.get('edit') ?? '' };
		} catch (cause) {
			if (cause instanceof AdminApiError) error(cause.status, cause.message);
			throw cause;
		}
	};

	export const actions: Actions = {
		save: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const pageCode = String(form.get('pageCode') ?? '');
			const tenantId = String(form.get('tenantId') ?? '');
			const permissions = form.getAll('permissions').map((value) => String(value));
			try {
				await saveAccessPage(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					pageCode,
					{ tenantId: tenantId === '' ? null : tenantId, permissions }
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/pages');
		},

		reset: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const pageCode = String(form.get('pageCode') ?? '');
			const tenantId = String(form.get('tenantId') ?? '');
			try {
				await saveAccessPage(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					pageCode,
					{
						tenantId: tenantId === '' ? null : tenantId,
						permissions: [],
						operation: 'reset'
					}
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/pages');
		}
	};
