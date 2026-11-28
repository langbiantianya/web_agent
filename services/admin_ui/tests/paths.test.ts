import { describe, expect, it } from 'vitest';
import { ADMIN_BASE, PYTHON_OWNED_PATHS, appPath, pythonPath } from '#lib/paths';

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

describe('Python 独占路径', () => {
	it('画布的实时校验端点必须独占', () => {
		// SvelteKit 的 /admin/workflows/[code] 会把 /admin/workflows/validate
		// 一起吃掉；不显式列出来，画布的校验请求就会打到 SvelteKit 路由上，
		// 表现为「校验没完成」而没有任何报错。
		expect(PYTHON_OWNED_PATHS).toContain('/workflows/validate');
	});

	it('登录与登出仍然归 Python', () => {
		expect(PYTHON_OWNED_PATHS).toEqual(
			expect.arrayContaining(['/login', '/auth', '/logout', '/static'])
		);
	});
});
