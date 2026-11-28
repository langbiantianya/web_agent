import { error } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';
import { AdminApiError, loadWorkflowDetail } from '#lib/server/admin-api';
import { pythonPath } from '#lib/paths';

/**
 * 工作流画布页（阶段 D）。
 *
 * **画布不重写**：`workflow-editor.js`（67 KB）仍旧是原来那套，Python 侧
 * 把它渲染成一段**不带外壳**的片段页；本页只负责画布外面的东西——导航、
 * 标题、返回链接——并用 `<iframe>` 把画布整块承载进来。
 *
 * 为什么要 iframe 而不是把片段直接拼进 DOM：画布脚本会遍历自己的 DOM、
 * 依赖 `admin.css` 里那 283 个 CSS 变量，而 SvelteKit 这一侧是 Tailwind
 * 令牌体系。iframe 给画布一个**隔离的文档**，两套样式不会互相污染——
 * 这是「不重写画布」能成立的前提。
 */
export const load: PageServerLoad = async ({ locals, params, request }) => {
	const session = locals.session;
	if (!session) error(401, '请先登录管理后台');
	const cookie = request.headers.get('cookie') ?? '';
	try {
		const item = await loadWorkflowDetail(cookie, params.code);
		return {
			workflow: item,
			canvasUrl: pythonPath(`/workflows/${encodeURIComponent(item.code)}/canvas`)
		};
	} catch (cause) {
		if (cause instanceof AdminApiError) error(cause.status, cause.message);
		throw cause;
	}
};
