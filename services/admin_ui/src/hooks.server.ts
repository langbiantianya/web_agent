/**
 * 请求入口。
 *
 * 这个文件是**新旧两套后台共存的仲裁点**。迁移分阶段进行（SvelteKit 页面与
 * Python 渲染的 Ludic 页面同时在线），所以每一个 `/admin/*` 请求都要先回答
 * 一个问题：**这个路径归谁处理？**
 *
 * 归属顺序（先匹配先生效）：
 *
 * 1. **Python 独占路径**（`/login`、`/auth`、`/logout`、`/static`）：反向代理。
 *    会话 Cookie、CSRF、Argon2 校验和审计都还在那边，代理时把 `Set-Cookie`
 *    原样带回浏览器，登录态才不会丢。
 * 2. **公开路径**（`/admin/health`）：本服务自己的存活探针。
 * 3. **未匹配到 SvelteKit 路由的路径**：回退给 Python。这条是**增量迁移的
 *    关键**——已迁的页面由 SvelteKit 渲染，没迁的仍然走旧的 Ludic 页面，
 *    用户看不出区别，也随时能把某个页面切回去。
 * 4. **已匹配的 SvelteKit 路由**：本服务处理。先装载会话，没登录就 302 到
 *    登录页。
 */

import { redirect } from '@sveltejs/kit';
import type { Handle } from '@sveltejs/kit/hooks';
import { loadSession } from '#lib/server/admin-api';
import { adminApiBaseUrl } from '#lib/server/config';
import { isOriginAllowed, pythonOrigin } from '#lib/server/origin';
import { pythonPath } from '#lib/paths';

/** 仍由 Python 独占的路径前缀（相对 base）。 */
const PYTHON_OWNED = ['/login', '/auth', '/static', '/logout'].map((path) => pythonPath(path));

/** 无需登录即可访问的路径（相对 base）。 */
const PUBLIC_PATHS = [pythonPath('/health')];

export const handle: Handle = async ({ event, resolve }) => {
	const path = event.url.pathname;

	// 1) Python 独占路径：直接转发
	if (PYTHON_OWNED.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))) {
		return await proxy(event);
	}

	// 2) 公开路径：不装载会话，也不跳转
	if (PUBLIC_PATHS.some((item) => item === path)) {
		return await resolve(event);
	}

	// 3) 没有匹配到 SvelteKit 路由：回退给 Python 的旧页面
	if (event.route.id === null) {
		return await proxy(event);
	}

	// 4) 装载会话
	const cookie = event.request.headers.get('cookie') ?? '';
	event.locals.session = await loadSession(cookie);

	// 5) 未登录：页面请求回登录页
	if (!event.locals.session && event.request.method === 'GET') {
		redirect(303, pythonPath('/login'));
	}

	return await resolve(event);
};

/** 把请求转发给 Python 管理端，并把响应原样带回。 */
async function proxy(event: Parameters<Handle>[0]['event']): Promise<Response> {
	const headers = new Headers();
	const cookie = event.request.headers.get('cookie');
	if (cookie) headers.set('cookie', cookie);
	const contentType = event.request.headers.get('content-type');
	if (contentType) headers.set('content-type', contentType);

	// Origin 重写：浏览器发的是公网地址，Python 看到的是内部地址，不改写
	// 就会被判成跨源。改写前先在这里校验浏览器的 Origin（见 origin.ts）。
	const origin = event.request.headers.get('origin');
	if (origin) {
		if (!isOriginAllowed(origin, event.url.origin)) {
			return new Response('请求来源无效', { status: 403 });
		}
		headers.set('origin', pythonOrigin(adminApiBaseUrl()));
	}

	const body =
		event.request.method === 'GET' || event.request.method === 'HEAD'
			? undefined
			: await event.request.arrayBuffer();

	const response = await fetch(`${adminApiBaseUrl()}${event.url.pathname}${event.url.search}`, {
		method: event.request.method,
		headers,
		body,
		redirect: 'manual'
	});

	const outHeaders = new Headers();
	for (const name of [
		'content-type',
		'location',
		'cache-control',
		'x-frame-options',
		'referrer-policy'
	]) {
		const value = response.headers.get(name);
		if (value) outHeaders.set(name, value);
	}
	// Set-Cookie 必须逐条透传：它带 Expires 属性，用逗号拼起来会被误解析。
	for (const cookie of response.headers.getSetCookie?.() ?? []) {
		outHeaders.append('set-cookie', cookie);
	}
	return new Response(response.body, { status: response.status, headers: outHeaders });
}