	import { error, fail, redirect } from '@sveltejs/kit';
	import type { ActionFailure } from '@sveltejs/kit';
	import type { Actions, PageServerLoad } from './$types';
	import {
		AdminApiError,
		createMapping,
		deleteMapping,
		loadMappings,
		updateMapping
	} from '#lib/server/admin-api';

	/** 表单错误载荷的形状。 */
	export interface MappingFormErrors {
		message: string;
		fieldErrors: Record<string, string>;
	}

	/** 把管理端 API 的失败翻译成表单错误。 */
	function toFailure(cause: unknown): ActionFailure<MappingFormErrors> {
		if (cause instanceof AdminApiError) {
			return fail(cause.status === 404 ? 404 : 400, {
				message: cause.message,
				fieldErrors: cause.fieldErrors
			});
		}
		throw cause;
	}

	export const load: PageServerLoad = async ({ locals, request }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		try {
			const payload = await loadMappings(request.headers.get('cookie') ?? '');
			return { payload };
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
			const providerId = Number(form.get('providerId') ?? '0');
			const claimName = String(form.get('claimName') ?? 'groups').trim();
			const externalValue = String(form.get('externalValue') ?? '').trim();
			const roleCode = String(form.get('roleCode') ?? '').trim();
			try {
				await createMapping(request.headers.get('cookie') ?? '', session.csrfToken, {
					providerId,
					claimName,
					externalValue,
					roleCode
				});
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/mappings');
		},

		update: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const mappingId = Number(form.get('mappingId') ?? '0');
			const roleCode = String(form.get('roleCode') ?? '').trim();
			const enabled = form.get('enabled') === 'on';
			try {
				await updateMapping(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					mappingId,
					{ roleCode, enabled }
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/mappings');
		},

		delete: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const mappingId = Number(form.get('mappingId') ?? '0');
			try {
				await deleteMapping(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					mappingId
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/access/mappings');
		}
	};
