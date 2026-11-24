/**
 * 运行期配置。
 *
 * **为什么直接读 `process.env` 而不是 `$app/env/private`**：Kit 3 的
 * `$app/env/private` 只导出**构建期声明过**的变量名（要在 `.env` 里列出），
 * 而这里的 `ADMIN_API_BASE_URL` 是**容器启动时**由编排注入的地址——两者
 * 生命周期不同。服务端代码本来就在 Node 里跑，直接读 `process.env` 最直接，
 * 也不会把内部地址打进客户端产物。
 */

/** 管理端（Python）基础地址。 */
export function adminApiBaseUrl(): string {
	const raw = process.env.ADMIN_API_BASE_URL ?? 'http://127.0.0.1:8001';
	return raw.replace(/\/+$/, '');
}