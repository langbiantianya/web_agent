import type { LayoutServerLoad } from './$types';
import { normalizeLocale } from '#lib/i18n';

/** 语言 Cookie 名。 */
const LOCALE_COOKIE = 'admin_locale';

export const load: LayoutServerLoad = async ({ locals, cookies }) => {
	const locale = normalizeLocale(cookies.get(LOCALE_COOKIE));
	return {
		session: locals.session,
		locale
	};
};