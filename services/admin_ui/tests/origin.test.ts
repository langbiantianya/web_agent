import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { allowedOrigins, isOriginAllowed, pythonOrigin } from '#lib/server/origin';

describe('isOriginAllowed', () => {
	const original = process.env.ADMIN_ALLOWED_ORIGINS;

	beforeEach(() => {
		process.env.ADMIN_ALLOWED_ORIGINS = '';
	});

	afterEach(() => {
		if (original === undefined) delete process.env.ADMIN_ALLOWED_ORIGINS;
		else process.env.ADMIN_ALLOWED_ORIGINS = original;
	});

	it('同源放行', () => {
		expect(isOriginAllowed('https://admin.example.com', 'https://admin.example.com')).toBe(true);
	});

	it('同源比较忽略结尾斜杠', () => {
		expect(isOriginAllowed('https://admin.example.com/', 'https://admin.example.com')).toBe(true);
	});

	it('跨源拒绝', () => {
		expect(isOriginAllowed('https://evil.example.com', 'https://admin.example.com')).toBe(false);
	});

	it('没有 Origin 头时放行（非浏览器写请求）', () => {
		expect(isOriginAllowed(null, 'https://admin.example.com')).toBe(true);
	});

	it('白名单里的跨源放行', () => {
		process.env.ADMIN_ALLOWED_ORIGINS = 'https://ops.example.com, https://qa.example.com';
		expect(isOriginAllowed('https://ops.example.com', 'https://admin.example.com')).toBe(true);
		expect(isOriginAllowed('https://qa.example.com', 'https://admin.example.com')).toBe(true);
		expect(isOriginAllowed('https://evil.example.com', 'https://admin.example.com')).toBe(false);
	});
});

describe('allowedOrigins', () => {
	it('空配置返回空数组', () => {
		process.env.ADMIN_ALLOWED_ORIGINS = '';
		expect(allowedOrigins()).toEqual([]);
	});

	it('去掉空项与结尾斜杠', () => {
		process.env.ADMIN_ALLOWED_ORIGINS = ' https://a.example.com/ , ,https://b.example.com ';
		expect(allowedOrigins()).toEqual(['https://a.example.com', 'https://b.example.com']);
	});
});

describe('pythonOrigin', () => {
	it('取管理端地址的 origin', () => {
		expect(pythonOrigin('http://admin-web:8001')).toBe('http://admin-web:8001');
		expect(pythonOrigin('http://admin-web:8001/')).toBe('http://admin-web:8001');
	});

	it('地址带路径时只保留 origin', () => {
		expect(pythonOrigin('http://admin-web:8001/admin')).toBe('http://admin-web:8001');
	});

	it('地址写坏时退回空串', () => {
		expect(pythonOrigin('not-a-url')).toBe('');
	});
});