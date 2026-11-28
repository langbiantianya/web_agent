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
