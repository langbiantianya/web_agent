/**
 * 路径工具。
 *
 * **为什么这里硬编码 `/admin`**：后台的公开路径前缀由 Python 侧决定
 * （`services/admin_web/registry.py` 里每个页面的 `route` 就是 `/admin/xxx`），
 * SvelteKit 侧的 `paths.base` 与它保持一致。硬编码一份常量，是为了让
 * 「指向 Python 页面的链接」与「SvelteKit 自己的路由」都能被类型检查覆盖，
 * 而不是散落着字符串拼接。
 */

/** 后台公开路径前缀。 */
export const ADMIN_BASE = '/admin';

/** 指向仍由 Python 提供的页面。 */
export function pythonPath(path: string): string {
	return `${ADMIN_BASE}${path.startsWith('/') ? path : `/${path}`}`;
}

/**
 * 指向 SvelteKit 自己的路由。
 *
 * 与 {@link pythonPath} 分开，是为了让「这条链接归谁处理」在代码里一眼
 * 看得出：Python 独占的页面（登录、OAuth 供应商、旧画布）走前者，
 * 已经迁过来的页面走后者。
 */
export function appPath(path: string): string {
	return `${ADMIN_BASE}${path.startsWith('/') ? path : `/${path}`}`;
}

/**
 * 仍由 Python **独占**的路径前缀（相对 base）。
 *
 * 这些路径不归 SvelteKit 管：登录、OAuth 回调、静态资源与登出走的是
 * Python 的会话体系；`/workflows/validate` 是画布 island 的**实时校验协议**
 * 端点。
 *
 * 最后一条尤其要留意：画布页是 `/admin/workflows/{code}`，SvelteKit 的
 * 动态路由 `/admin/workflows/[code]` 会把 `/admin/workflows/validate`
 * 一并吃掉——那正是画布提交校验请求的地址。撞上了，校验就会静默失败
 * （画布上只剩一句「校验没完成」）。所以它必须显式列在这里。
 */
export const PYTHON_OWNED_PATHS = [
	'/login',
	'/auth',
	'/static',
	'/logout',
	'/workflows/validate'
];
