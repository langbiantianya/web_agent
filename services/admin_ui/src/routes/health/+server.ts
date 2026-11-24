import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

/**
 * 健康检查。
 *
 * **刻意不查后端**：这个端点只回答「本容器还活着并且能处理请求」。
 * 管理端数据库、admin-web 容器是否健康由它们各自的探针负责——把两件事
 * 揉在一个探针里，故障时会分不清是谁挂了。
 */
export const GET: RequestHandler = async () =>
	json({ status: 'ok', service: 'admin-ui' });