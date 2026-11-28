<script lang="ts">
	import { enhance } from '$app/forms';
	import Field from '../../lib/components/Field.svelte';
	import Panel from '../../lib/components/Panel.svelte';
	import Toolbar from '../../lib/components/Toolbar.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);
	const editing = $derived(
		payload.items.find((item) => item.code === data.editing) ?? null
	);

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
	const sorts = $derived([
		{ value: '', label: t(locale, 'skills.sort.default') },
		{ value: 'name', label: t(locale, 'skills.sort.name') },
		{ value: 'code', label: t(locale, 'skills.sort.code') },
		{ value: 'created', label: t(locale, 'skills.sort.created') }
	]);
</script>

<svelte:head>
	<title>{t(locale, 'skills.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'skills.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'skills.description')}</p>
	</header>

	{#if form?.message}
		<p class="rounded-card border border-danger bg-surface px-3 py-2 text-sm text-danger">
			{form.message}
		</p>
	{/if}

	<Panel>
		<Toolbar
			q={payload.page.q}
			sort={payload.page.sort}
			page={payload.page.page}
			total={payload.total}
			size={payload.page.size}
			{sorts}
			searchLabel={t(locale, 'common.search')}
			sortLabel={t(locale, 'common.sort')}
			applyLabel={t(locale, 'common.apply')}
			summary={t(locale, 'common.total', { count: payload.total })}
			prevLabel={t(locale, 'common.prev')}
			nextLabel={t(locale, 'common.next')}
		/>
	</Panel>

	<Panel>
		{#if payload.items.length === 0}
			<p class="text-sm text-ink-muted">{t(locale, 'common.empty')}</p>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full min-w-[720px] text-left text-sm">
					<thead class="text-xs uppercase tracking-wider text-ink-muted">
						<tr>
							<th class="pb-2 pr-4">{t(locale, 'common.code')}</th>
							<th class="pb-2 pr-4">{t(locale, 'agents.field.description')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.status')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.usedBy')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.code)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4 font-mono text-xs">{item.code}</td>
								<td class="py-2 pr-4">{item.description}</td>
								<td class="py-2 pr-4">
									{t(locale, `skills.status.${item.status}` as 'skills.status.published')}
								</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">
									{item.usedBy.join(', ') || '—'}
								</td>
								<td class="py-2 pr-4">
									<div class="flex items-center gap-2">
										<a
											class="rounded-card border border-line px-2 py-1 text-xs"
											href="?edit={encodeURIComponent(item.code)}"
										>
											{t(locale, 'common.edit')}
										</a>
										<form method="POST" action="?/archive" use:enhance>
											<input type="hidden" name="code" value={item.code} />
											<button class="rounded-card border border-line px-2 py-1 text-xs" type="submit">
												{t(locale, 'common.archive')}
											</button>
										</form>
									</div>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</Panel>

	{#if editing}
		<Panel title={t(locale, 'common.edit')}>
			<form class="flex flex-col gap-3" method="POST" action="?/update" use:enhance>
				<input type="hidden" name="code" value={editing.code} />
				<Field
					label={t(locale, 'agents.field.description')}
					name="description"
					value={editing.description}
					required
					error={fieldErrors.description}
				/>
				<Field
					label={t(locale, 'skills.field.allowedTools')}
					name="allowedTools"
					value={editing.allowedTools.join(', ')}
					error={fieldErrors.allowed_tools}
				/>
				<Field
					label={t(locale, 'skills.field.body')}
					name="body"
					value={editing.body}
					textarea
					rows={8}
					error={fieldErrors.body}
				/>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<label class="flex flex-col gap-1 text-sm">
						<span class="font-medium">{t(locale, 'skills.field.status')}</span>
						<select
							class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
							name="status"
						>
							<option value="published" selected={editing.status === 'published'}>
								{t(locale, 'skills.status.published')}
							</option>
							<option value="draft" selected={editing.status === 'draft'}>
								{t(locale, 'skills.status.draft')}
							</option>
						</select>
					</label>
					<label class="flex items-end gap-2 text-sm">
						<input type="checkbox" name="enabled" checked={editing.enabled} />
						<span>{t(locale, 'skills.field.enabled')}</span>
					</label>
				</div>
				<div class="flex gap-2">
					<button
						class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink"
						type="submit"
					>
						{t(locale, 'common.save')}
					</button>
					<a class="rounded-card border border-line px-3 py-2 text-sm" href="/admin/skills">
						{t(locale, 'common.cancel')}
					</a>
				</div>
			</form>
		</Panel>
	{:else}
		<Panel title={t(locale, 'common.create')}>
			<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<Field
						label={t(locale, 'skills.field.code')}
						name="code"
						required
						error={fieldErrors.code}
					/>
					<Field
						label={t(locale, 'agents.field.description')}
						name="description"
						required
						error={fieldErrors.description}
					/>
				</div>
				<Field
					label={t(locale, 'skills.field.allowedTools')}
					name="allowedTools"
					error={fieldErrors.allowed_tools}
				/>
				<Field
					label={t(locale, 'skills.field.body')}
					name="body"
					textarea
					rows={8}
					error={fieldErrors.body}
				/>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<label class="flex flex-col gap-1 text-sm">
						<span class="font-medium">{t(locale, 'skills.field.status')}</span>
						<select
							class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
							name="status"
						>
							<option value="published">{t(locale, 'skills.status.published')}</option>
							<option value="draft">{t(locale, 'skills.status.draft')}</option>
						</select>
					</label>
					<label class="flex items-end gap-2 text-sm">
						<input type="checkbox" name="enabled" checked />
						<span>{t(locale, 'skills.field.enabled')}</span>
					</label>
				</div>
				<div class="flex gap-2">
					<button
						class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink"
						type="submit"
					>
						{t(locale, 'common.create')}
					</button>
				</div>
			</form>
		</Panel>
	{/if}
</section>
