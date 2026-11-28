import { error, fail, redirect } from '@sveltejs/kit';
import type { ActionFailure } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
	AdminApiError,
	createOAuthProvider,
	deleteOAuthProvider,
	loadOAuthProviders,
	toggleOAuthProvider,
	updateOAuthProvider
} from '#lib/server/admin-api';

/** 表单错误载荷的形状。 */
export interface OAuthProviderFormErrors {
	message: string;
	fieldErrors: Record<string, string>;
}

/** 把表单读成供应商写入用的形状。 */
function readInput(form: FormData): Record<string, unknown> {
	const scopes = String(form.get('scopes') ?? '')
		.split(/\s+/)
		.filter((item) => item !== '');
	return {
		displayName: String(form.get('displayName') ?? '').trim(),
		issuer: String(form.get('issuer') ?? '').trim(),
		clientId: String(form.get('clientId') ?? '').trim(),
		clientSecret: String(form.get('clientSecret') ?? ''),
		discoveryUrl: String(form.get('discoveryUrl') ?? '').trim(),
		authorizationEndpoint: String(form.get('authorizationEndpoint') ?? '').trim(),
		tokenEndpoint: String(form.get('tokenEndpoint') ?? '').trim(),
		jwksUri: String(form.get('jwksUri') ?? '').trim(),
		tokenAuthMethod: String(form.get('tokenAuthMethod') ?? 'client_secret_basic'),
		scopes,
		usernameClaim: String(form.get('usernameClaim') ?? '').trim(),
		roleClaim: String(form.get('roleClaim') ?? '').trim(),
		roleSyncMaxAgeSeconds: Number(form.get('roleSyncMaxAgeSeconds') ?? '1800'),
		clientLoginEnabled: form.get('clientLoginEnabled') === 'on',
		adminLoginEnabled: form.get('adminLoginEnabled') === 'on',
		roleClaimRequired: form.get('roleClaimRequired') === 'on',
		autoCreate: form.get('autoCreate') === 'on',
		enabled: form.get('enabled') === 'on'
	};
}

/** 把管理端 API 的失败翻译成表单错误。 */
function toFailure(cause: unknown): ActionFailure<OAuthProviderFormErrors> {
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
		const payload = await loadOAuthProviders(cookie);
		return {
			payload,
			editing: url.searchParams.get('edit') ?? ''
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
		try {
			await createOAuthProvider(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				{ ...readInput(form), code: String(form.get('code') ?? '').trim() }
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/oauth-providers');
	},

	update: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		const code = String(form.get('code') ?? '');
		const input = readInput(form);
		// 密钥是**只写**字段：留空 = 保持原值，勾选清除 = 清空。
		if (input.clientSecret === '' && form.get('clearSecret') !== 'on') {
			delete input.clientSecret;
		}
		if (form.get('clearSecret') === 'on') {
			input.clientSecret = '';
		}
		try {
			await updateOAuthProvider(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				code,
				input
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/oauth-providers');
	},

	toggle: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		try {
			await toggleOAuthProvider(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				String(form.get('code') ?? ''),
				form.get('enabled') === 'on'
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/oauth-providers');
	},

	delete: async ({ request, locals }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const form = await request.formData();
		try {
			await deleteOAuthProvider(
				request.headers.get('cookie') ?? '',
				session.csrfToken,
				String(form.get('code') ?? '')
			);
		} catch (cause) {
			return toFailure(cause);
		}
		redirect(303, '/admin/oauth-providers');
	}
};
