	import { error } from '@sveltejs/kit';
	import type { PageServerLoad } from './$types';
	import { AdminApiError, loadAudit } from '#lib/server/admin-api';

	export const load: PageServerLoad = async ({ locals, request, url }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		try {
			const payload = await loadAudit(request.headers.get('cookie') ?? '', {
				runId: url.searchParams.get('runId') ?? '',
				toolName: url.searchParams.get('toolName') ?? '',
				source: url.searchParams.get('source') ?? '',
				ok: url.searchParams.get('ok') ?? '',
				page: Number(url.searchParams.get('page') ?? '1') || 1,
				size: Number(url.searchParams.get('size') ?? '20') || 20
			});
			return { payload };
		} catch (cause) {
			if (cause instanceof AdminApiError) error(cause.status, cause.message);
			throw cause;
		}
	};
