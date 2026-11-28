/**
 * 同源校验与 Origin 重写。
 *
 * **问题**：浏览器发来的 `Origin` 是**公网地址**（比如
 * `https://admin.example.com`），而 Python 管理端看到的是**内部地址**
 * （`http://admin-web:8001`）。Python 的 `origin_ok()` 拿 Origin 和
 * 「自己的 scheme://Host」比，于是代理过来的请求一律被判成跨源，登录直接
 * 失败。
 *
 * **解法是分层校验，而不是放行**：
 *
 * 1. **浏览器 → SvelteKit**：由本模块校验。Origin 必须等于应用自身 origin
 *    （`event.url.origin`，受 SvelteKit 的 `ORIGIN` 环境变量影响，因此
 *    HTTPS 终止在反代后面也能算对），或落在显式白名单里。不通过就 403。
 * 2. **SvelteKit → Python**：把 `Origin` **重写成 Python 自己的 origin**。
 *    Python 的同源检查因此照常通过——它拦的是「绕过本服务、直连管理端」的
 *    请求，那条路径上 Origin 仍是浏览器真实地址，与 Host 对不上，照样被拒。
 *
 * 这样两侧的检查都不失效：**Python 继续防直连，SvelteKit 负责防跨源**。
 */

/** 允许跨源写请求的额外 Origin 白名单（逗号分隔）。 */
export function allowedOrigins(): string[] {
	const raw = process.env.ADMIN_ALLOWED_ORIGINS ?? '';
	return raw
		.split(',')
		.map((item) => item.trim().replace(/\/+$/, ''))
		.filter((item) => item.length > 0);
}

/**
 * 判断浏览器发来的 Origin 是否可以写。
 *
 * @param origin 浏览器发来的 Origin 头（可能为空）。
 * @param selfOrigin 应用自身的 origin。
 */
export function isOriginAllowed(origin: string | null, selfOrigin: string): boolean {
	// 没有 Origin 头的不是浏览器写请求（例如 curl），按既有语义放行。
	if (!origin) return true;
	const normalized = origin.replace(/\/+$/, '');
	if (normalized === selfOrigin.replace(/\/+$/, '')) return true;
	return allowedOrigins().includes(normalized);
}

/**
 * 管理端当前的同源校验策略。
 *
 * - ``allowlist``：Python 按 ``AUTH_ALLOWED_ORIGINS`` 白名单比对；
 * - ``host``：Python 按请求自身的 ``scheme://Host`` 比对。
 */
export interface OriginPolicy {
	mode: 'allowlist' | 'host';
	origins: string[];
}

/**
 * 决定转发给 Python 时该带哪个 Origin。
 *
 * **两种模式的改写方向是相反的**：白名单模式下 Python 只认白名单里的
 * 地址，而浏览器的 Origin 正是管理端的公开地址（管理员能登录，就说明它
 * 在白名单里），所以**原样转发**；Host 模式下 Python 拿 Origin 与自己看到
 * 的 ``scheme://Host`` 比，而转发时它看到的是内部地址，所以要**改写成内部
 * 地址**。方向搞反，两种模式下都会把写请求全部拒掉。
 *
 * @param browserOrigin 浏览器发来的 Origin（可能为空）。
 * @param policy 管理端当前的校验策略。
 * @param python 管理端自己的 origin（内部地址）。
 */
export function forwardOrigin(
	browserOrigin: string | null,
	policy: OriginPolicy,
	python: string
): string | null {
	if (!browserOrigin) return null;
	if (policy.mode === 'allowlist') return browserOrigin;
	return python || browserOrigin;
}

/**
 * 取转发给 Python 时应使用的 Origin。
 *
 * 用管理端自己的 origin，让 Python 的 `origin_ok()` 比较相等。
 *
 * @param adminApiBaseUrl 管理端基础地址。
 */
export function pythonOrigin(adminApiBaseUrl: string): string {
	try {
		return new URL(adminApiBaseUrl).origin;
	} catch {
		// 地址写坏时退回空串：不带 Origin 头，Python 侧按「无 Origin」放行，
		// 而不是拿一个半截地址去比较。
		return '';
	}
}