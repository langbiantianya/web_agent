import { describe, expect, it } from 'vitest';
import { DEFAULT_LOCALE, DICTIONARIES, LOCALES, groupTitle, normalizeLocale, pageTitle, t } from '#lib/i18n';

describe('normalizeLocale', () => {
	it('把中文的各种写法归一到 zh-CN', () => {
		expect(normalizeLocale('zh')).toBe('zh-CN');
		expect(normalizeLocale('zh-CN')).toBe('zh-CN');
		expect(normalizeLocale('zh-Hans-CN')).toBe('zh-CN');
	});

	it('其它语言落到默认语言', () => {
		expect(normalizeLocale('en-US')).toBe(DEFAULT_LOCALE);
		expect(normalizeLocale(null)).toBe(DEFAULT_LOCALE);
		expect(normalizeLocale('')).toBe(DEFAULT_LOCALE);
	});
});

describe('词典', () => {
	it('每种语言的键集合完全一致', () => {
		// 漏翻在这里就会炸：两种语言各写一遍，少一个键就会对不上。
		const [first, ...rest] = LOCALES;
		const expected = Object.keys(DICTIONARIES[first]).sort();
		for (const locale of rest) {
			expect(Object.keys(DICTIONARIES[locale]).sort()).toEqual(expected);
		}
		expect(expected.length).toBeGreaterThan(0);
	});

	it('中文与英文都能取到文案', () => {
		expect(t('en', 'nav.agents')).toBe('Agents');
		expect(t('zh-CN', 'nav.agents')).toBe('智能体');
	});
});

describe('导航标题', () => {
	it('页面 code 能翻译', () => {
		expect(pageTitle('zh-CN', 'agents', 'Agents')).toBe('智能体');
	});

	it('未登记的 code 退回服务端标题', () => {
		expect(pageTitle('zh-CN', 'unknown_code', 'Fallback')).toBe('Fallback');
	});

	it('分组标题能翻译', () => {
		expect(groupTitle('zh-CN', 'Resources', 'Resources')).toBe('资源');
	});
});