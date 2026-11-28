import { describe, expect, it } from 'vitest';
import { ADMIN_BASE, appPath, pythonPath } from '#lib/paths';

describe('后台路径', () => {
	it('Python 独占页面走 pythonPath', () => {
		expect(pythonPath('/login')).toBe('/admin/login');
		expect(pythonPath('login')).toBe('/admin/login');
	});

	it('SvelteKit 自有路由走 appPath', () => {
		expect(appPath('/workflows')).toBe('/admin/workflows');
		expect(appPath('workflows')).toBe('/admin/workflows');
	});

	it('两条前缀一致，区别只在归属', () => {
		// 同一个前缀下的路径由 SvelteKit 的 base 决定；写错会让链接落到
		// 站点根目录去。
		expect(appPath('/workflows')).toBe(`${ADMIN_BASE}/workflows`);
	});
});
