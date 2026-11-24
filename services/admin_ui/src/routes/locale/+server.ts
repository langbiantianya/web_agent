import { redirect, type RequestHandler } from '@sveltejs/kit';
import { pythonPath, ADMIN_BASE } from '#lib/paths';
import { normalizeLocale } from '#lib/i18n';

/** 语言 Cookie 名。 */
const LOCALE_COOKIE = 'admin_locale';

/** 切换界面语言：写 Cookie 后回到来源页。 */
export const POST: RequestHandler = async ({ request, cookies }) => {
	const form = await request.formData();
	const locale = normalizeLocale(String(form.get('locale') ?? ''));
	cookies.set(LOCALE_COOKIE, locale, { path: ADMIN_BASE, httpOnly: false, sameSite: 'lax' });
	const back = String(form.get('back') ?? '');
	const root = `${ADMIN_BASE}/`;
	redirect(303, back.startsWith(root) ? back : root);
};