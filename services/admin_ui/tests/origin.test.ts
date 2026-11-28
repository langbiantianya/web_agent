import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import {
	allowedOrigins,
	forwardOrigin,
	isOriginAllowed,
	pythonOrigin
} from '#lib/server/origin';

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

describe('forwardOrigin', () => {
	it('白名单模式原样转发浏览器的 Origin', () => {
		// 白名单里的都是管理端的公开地址；浏览器发来的就是它，原样转发
		// 才能过 Python 的白名单校验。
		expect(
			forwardOrigin(
				'http://192.168.5.16:8001',
				{ mode: 'allowlist', origins: ['http://192.168.5.16:8001'] },
				'http://admin-web:8001'
			)
		).toBe('http://192.168.5.16:8001');
	});

	it('Host 模式改写成管理端自己的 origin', () => {
		// Python 拿 Origin 与自己看到的 scheme://Host 比，转发时它看到的是
		// 内部地址，所以要改写成内部地址。
		expect(
			forwardOrigin(
				'http://192.168.5.16:8001',
				{ mode: 'host', origins: [] },
				'http://admin-web:8001'
			)
		).toBe('http://admin-web:8001');
	});

	it('没有 Origin 头时不带 Origin', () => {
		expect(
			forwardOrigin(null, { mode: 'host', origins: [] }, 'http://admin-web:8001')
		).toBeNull();
	});

	it('Host 模式但管理端地址写坏时退回浏览器 Origin', () => {
		expect(
			forwardOrigin('http://192.168.5.16:8001', { mode: 'host', origins: [] }, '')
		).toBe('http://192.168.5.16:8001');
	});
});
