import { error } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';
import { AdminApiError, loadDashboard } from '#lib/server/admin-api';

export const load: PageServerLoad = async ({ locals, request }) => {
	const session = locals.session;
	if (!session) {
		error(401, '请先登录管理后台');
	}
	try {
		const payload = await loadDashboard(request.headers.get('cookie') ?? '', session.csrfToken);
		return { payload };
	} catch (cause) {
		if (cause instanceof AdminApiError) {
			error(cause.status, cause.message);
		}
		throw cause;
	}
};