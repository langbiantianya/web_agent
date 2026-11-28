	import { error, fail, redirect } from '@sveltejs/kit';
	import type { ActionFailure } from '@sveltejs/kit';
	import type { Actions, PageServerLoad } from './$types';
	import {
		AdminApiError,
		createRole,
		deleteRole,
		loadRoles,
		updateRole,
		updateRoleInheritance
	} from '#lib/server/admin-api';

	/** 表单错误载荷的形状。 */
	export interface RoleFormErrors {
		message: string;
		fieldErrors: Record<string, string>;
	}

	/** 把管理端 API 的失败翻译成表单错误。 */
	function toFailure(cause: unknown): ActionFailure<RoleFormErrors> {
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
			const payload = await loadRoles(request.headers.get('cookie') ?? '', {
				q: url.searchParams.get('q') ?? '',
				sort: url.searchParams.get('sort') ?? '',
				page: Number(url.searchParams.get('page') ?? '1') || 1,
				size: Number(url.searchParams.get('size') ?? '20') || 20
			});
			return {
				payload,
				editing: url.searchParams.get('edit') ?? '',
				editingDomain: url.searchParams.get('domain') ?? ''
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
			const domain = String(form.get('domain') ?? '').trim();
			const permissions = form.getAll('permissions').map((value) => String(value));
			try {
				await createRole(request.headers.get('cookie') ?? '', session.csrfToken, {
					code,
					domain,
					permissions
				});
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/roles');
		},

		update: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const roleCode = String(form.get('roleCode') ?? '').trim();
			const domain = String(form.get('domain') ?? '').trim();
			const permissions = form.getAll('permissions').map((value) => String(value));
			try {
				await updateRole(request.headers.get('cookie') ?? '', session.csrfToken, roleCode, {
					domain,
					permissions
				});
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/roles');
		},

		inheritance: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const roleCode = String(form.get('roleCode') ?? '').trim();
			const domain = String(form.get('domain') ?? '').trim();
			const parents = form.getAll('parents').map((value) => String(value));
			try {
				await updateRoleInheritance(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					roleCode,
					{ domain, parents }
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/roles');
		},

		delete: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const roleCode = String(form.get('roleCode') ?? '').trim();
			const domain = String(form.get('domain') ?? '').trim();
			try {
				await deleteRole(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					roleCode,
					domain
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/roles');
		}
	};
