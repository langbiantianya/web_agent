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
		{ value: '', label: t(locale, 'llm.sort.default') },
		{ value: 'name', label: t(locale, 'llm.sort.name') },
		{ value: 'code', label: t(locale, 'llm.sort.code') },
		{ value: 'priority', label: t(locale, 'llm.sort.priority') },
		{ value: 'created', label: t(locale, 'llm.sort.created') }
	]);
</script>

<svelte:head>
	<title>{t(locale, 'llm.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'llm.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'llm.description')}</p>
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
							<th class="pb-2 pr-4">{t(locale, 'common.name')}</th>
							<th class="pb-2 pr-4">{t(locale, 'llm.column.baseUrl')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.model')}</th>
							<th class="pb-2 pr-4">{t(locale, 'llm.column.priority')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.code)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4 font-mono text-xs">{item.code}</td>
								<td class="py-2 pr-4">{item.name}</td>
								<td class="py-2 pr-4 font-mono text-xs">{item.baseUrl}</td>
								<td class="py-2 pr-4">{item.model || '—'}</td>
								<td class="py-2 pr-4">{item.priority}</td>
								<td class="py-2 pr-4">
									<div class="flex items-center gap-2">
										<form method="POST" action="?/toggle" use:enhance>
											<input type="hidden" name="code" value={item.code} />
											<input type="hidden" name="enabled" value={item.enabled ? 'off' : 'on'} />
											<button class="rounded-card border border-line px-2 py-1 text-xs" type="submit">
												{item.enabled
													? t(locale, 'common.enabled')
													: t(locale, 'common.disabled')}
											</button>
										</form>
										<a
											class="rounded-card border border-line px-2 py-1 text-xs"
											href="?edit={encodeURIComponent(item.code)}"
										>
											{t(locale, 'common.edit')}
										</a>
										<form method="POST" action="?/delete" use:enhance>
											<input type="hidden" name="code" value={item.code} />
											<button
												class="rounded-card border border-danger px-2 py-1 text-xs text-danger"
												type="submit"
											>
												{t(locale, 'common.delete')}
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
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<Field
						label={t(locale, 'llm.field.name')}
						name="name"
						value={editing.name}
						error={fieldErrors.name}
					/>
					<Field
						label={t(locale, 'llm.field.baseUrl')}
						name="baseUrl"
						value={editing.baseUrl}
						required
						error={fieldErrors.baseUrl}
					/>
				</div>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<Field
						label={t(locale, 'llm.field.model')}
						name="model"
						value={editing.model}
						error={fieldErrors.model}
					/>
					<Field
						label={t(locale, 'llm.field.priority')}
						name="priority"
						type="number"
						value={String(editing.priority)}
						error={fieldErrors.priority}
					/>
				</div>
				<Field
					label={t(locale, 'llm.field.apiKey')}
					name="apiKey"
					type="password"
					hint={t(locale, 'llm.apiKey.hint')}
					error={fieldErrors.apiKey}
				/>
				<label class="flex items-center gap-2 text-sm">
					<input type="checkbox" name="enabled" checked={editing.enabled} />
					<span>{t(locale, 'llm.field.enabled')}</span>
				</label>
				<div class="flex gap-2">
					<button
						class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink"
						type="submit"
					>
						{t(locale, 'common.save')}
					</button>
					<a class="rounded-card border border-line px-3 py-2 text-sm" href="/admin/llm">
						{t(locale, 'common.cancel')}
					</a>
				</div>
			</form>
		</Panel>
	{:else}
		<Panel title={t(locale, 'common.create')}>
			<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-3">
					<Field
						label={t(locale, 'llm.field.code')}
						name="code"
						required
						error={fieldErrors.code}
					/>
					<Field
						label={t(locale, 'llm.field.name')}
						name="name"
						error={fieldErrors.name}
					/>
					<Field
						label={t(locale, 'llm.field.priority')}
						name="priority"
						type="number"
						value="100"
						error={fieldErrors.priority}
					/>
				</div>
				<Field
					label={t(locale, 'llm.field.baseUrl')}
					name="baseUrl"
					required
					error={fieldErrors.baseUrl}
				/>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<Field
						label={t(locale, 'llm.field.model')}
						name="model"
						error={fieldErrors.model}
					/>
					<Field
						label={t(locale, 'llm.field.apiKey')}
						name="apiKey"
						type="password"
						error={fieldErrors.apiKey}
					/>
				</div>
				<label class="flex items-center gap-2 text-sm">
					<input type="checkbox" name="enabled" checked />
					<span>{t(locale, 'llm.field.enabled')}</span>
				</label>
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