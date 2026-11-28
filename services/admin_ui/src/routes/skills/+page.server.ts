	import { error, fail, redirect } from '@sveltejs/kit';
	import type { ActionFailure } from '@sveltejs/kit';
	import type { Actions, PageServerLoad } from './$types';
	import {
		AdminApiError,
		archiveSkill,
		createSkill,
		loadSkills,
		updateSkill
	} from '#lib/server/admin-api';
	import type { SkillInput } from '#lib/types';

	/** 表单错误载荷的形状。 */
	export interface SkillFormErrors {
		message: string;
		fieldErrors: Record<string, string>;
	}

	/** 把表单读成技能写入用的形状。 */
	function readInput(form: FormData): SkillInput {
		return {
			description: String(form.get('description') ?? ''),
			allowedTools: String(form.get('allowedTools') ?? '')
				.split(',')
				.map((item) => item.trim())
				.filter(Boolean),
			body: String(form.get('body') ?? ''),
			status: form.get('status') === 'draft' ? 'draft' : 'published',
			enabled: form.get('enabled') === 'on'
		};
	}

	/** 把管理端 API 的失败翻译成表单错误。 */
	function toFailure(cause: unknown): ActionFailure<SkillFormErrors> {
		if (cause instanceof AdminApiError) {
			const fieldErrors = (
				cause as AdminApiError & { fieldErrors?: Record<string, string> }
			).fieldErrors;
			return fail(cause.status === 404 ? 404 : 400, {
				message: cause.message,
				fieldErrors: fieldErrors ?? {}
			});
		}
		throw cause;
	}

	export const load: PageServerLoad = async ({ locals, request, url }) => {
		const session = locals.session;
		if (!session) error(401, '请先登录管理后台');
		const cookie = request.headers.get('cookie') ?? '';
		try {
			const payload = await loadSkills(cookie, {
				q: url.searchParams.get('q') ?? '',
				sort: url.searchParams.get('sort') ?? '',
				page: Number(url.searchParams.get('page') ?? '1') || 1,
				size: Number(url.searchParams.get('size') ?? '20') || 20
			});
			return { payload, editing: url.searchParams.get('edit') ?? '' };
		} catch (cause) {
			if (cause instanceof AdminApiError) error(cause.status, cause.message);
			throw cause;
		}
	};

	export const actions: Actions = {
		create: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const code = String(form.get('code') ?? '').trim();
			try {
				await createSkill(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					{ ...readInput(form), code }
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/skills');
		},

		update: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const code = String(form.get('code') ?? '').trim();
			try {
				await updateSkill(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					code,
					readInput(form)
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/skills');
		},

		archive: async ({ request, locals }) => {
			const session = locals.session;
			if (!session) error(401, '请先登录管理后台');
			const form = await request.formData();
			const code = String(form.get('code') ?? '');
			try {
				await archiveSkill(
					request.headers.get('cookie') ?? '',
					session.csrfToken,
					code
				);
			} catch (cause) {
				return toFailure(cause);
			}
			redirect(303, '/admin/skills');
		}
	};
