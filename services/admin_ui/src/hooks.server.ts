/**
 * 请求入口。
 *
 * 两条职责：
 *
 * 1. **代理仍由 Python 拥有的路径**：登录页、OAuth 回调、登出、旧后台的静态
 *    资源。这些路径必须原样打到 admin-web——会话 Cookie、CSRF、Argon2 校验和
 *    审计都还在那边，代理时把 `Set-Cookie` 原样带回浏览器，登录态才不会丢。
 * 2. **装载会话**：每个页面请求先问 Python「我是谁、能看哪些页」，把结果放进
 *    `locals.session`。没登录时页面请求 302 到登录页。
 */

import { redirect } from '@sveltejs/kit';
import type { Handle } from '@sveltejs/kit/hooks';
import { loadSession } from '#lib/server/admin-api';
import { adminApiBaseUrl } from '#lib/server/config';
import { pythonPath } from '#lib/paths';

/** 仍由 Python 独占的路径前缀（相对 base）。 */
const PYTHON_OWNED = ['/login', '/auth', '/static', '/logout'].map((path) => pythonPath(path));

/** 无需登录即可访问的路径（相对 base）。 */
const PUBLIC_PATHS = [pythonPath('/health')];

export const handle: Handle = async ({ event, resolve }) => {
	const path = event.url.pathname;

	// 1) 代理：路径落在 Python 侧就直接转发
	if (PYTHON_OWNED.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))) {
		return await proxy(event);
	}

	// 2) 公开路径：不装载会话，也不跳转
	if (PUBLIC_PATHS.some((path) => path === event.url.pathname)) {
		return await resolve(event);
	}

	// 3) 装载会话
	const cookie = event.request.headers.get('cookie') ?? '';
	event.locals.session = await loadSession(cookie);

	// 4) 未登录：页面请求回登录页
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
	const origin = event.request.headers.get('origin');
	if (origin) headers.set('origin', origin);

	const body = event.request.method === 'GET' || event.request.method === 'HEAD' ? undefined : await event.request.arrayBuffer();

	const response = await fetch(`${adminApiBaseUrl()}${event.url.pathname}${event.url.search}`, {
		method: event.request.method,
		headers,
		body,
		redirect: 'manual'
	});

	const outHeaders = new Headers();
	for (const name of ['content-type', 'location', 'cache-control', 'x-frame-options', 'referrer-policy']) {
		const value = response.headers.get(name);
		if (value) outHeaders.set(name, value);
	}
	for (const cookie of response.headers.getSetCookie?.() ?? []) {
		outHeaders.append('set-cookie', cookie);
	}
	return new Response(response.body, { status: response.status, headers: outHeaders });
}