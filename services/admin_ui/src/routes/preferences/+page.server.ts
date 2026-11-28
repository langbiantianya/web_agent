	import { error, fail, redirect } from '@sveltejs/kit';
	import type { ActionFailure } from '@sveltejs/kit';
	import type { Actions, PageServerLoad } from './$types';
	import { AdminApiError, loadPreferences, savePreferences } from '#lib/server/admin-api';

	/** 表单错误载荷的形状。 */
	export interface PreferenceFormErrors {
		message: string;
		fieldErrors: Record<string, string>;
	}

	/** 把管理端 API 的失败翻译成表单错误。 */
	function toFailure(cause: unknown): ActionFailure<PreferenceFormErrors> {
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
			const payload = await loadPreferences(request.headers.get('cookie') ?? '');
			return { payload };
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
			const theme = String(form.get('theme') ?? 'fluent');
			const colorMode = String(form.get('colorMode') ?? 'system');
			const density = String(form.get('density') ?? 'comfortable');
			try {
				await savePreferences(request.headers.get('cookie') ?? '', session.csrfToken, {
					theme,
					colorMode,
					density
				});
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/preferences');
		}
	};
