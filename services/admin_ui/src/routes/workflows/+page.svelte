<script lang="ts">
	import { enhance } from '$app/forms';
	import Field from '../../lib/components/Field.svelte';
	import Panel from '../../lib/components/Panel.svelte';
	import Toolbar from '../../lib/components/Toolbar.svelte';
	import { pythonPath } from '#lib/paths';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
	const sorts = $derived([
		{ value: '', label: t(locale, 'workflows.sort.default') },
		{ value: 'name', label: t(locale, 'workflows.sort.name') },
		{ value: 'code', label: t(locale, 'workflows.sort.code') }
	]);
</script>

<svelte:head>
	<title>{t(locale, 'workflows.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'workflows.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'workflows.description')}</p>
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
							<th class="pb-2 pr-4">{t(locale, 'workflows.column.nodes')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.code)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4 font-mono text-xs">{item.code}</td>
								<td class="py-2 pr-4">{item.name}</td>
								<td class="py-2 pr-4">{item.nodeCount}</td>
								<td class="py-2 pr-4">
									<div class="flex items-center gap-2">
										<a
											class="rounded-card border border-line px-2 py-1 text-xs"
											href={pythonPath(`/workflows/${encodeURIComponent(item.code)}`)}
										>
											{t(locale, 'common.edit')}
										</a>
										<form method="POST" action="?/toggle" use:enhance>
											<input type="hidden" name="code" value={item.code} />
											<input type="hidden" name="enabled" value={item.enabled ? 'off' : 'on'} />
											<button class="rounded-card border border-line px-2 py-1 text-xs" type="submit">
												{item.enabled
													? t(locale, 'common.enabled')
													: t(locale, 'common.disabled')}
											</button>
										</form>
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

	<Panel title={t(locale, 'common.create')}>
		<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
			<div class="grid grid-cols-1 gap-3 md:grid-cols-3">
				<Field
					label={t(locale, 'workflows.field.code')}
					name="code"
					required
					error={fieldErrors.code}
				/>
				<Field
					label={t(locale, 'workflows.field.name')}
					name="name"
					error={fieldErrors.name}
				/>
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'workflows.field.agent')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="agentCode"
					>
						{#each payload.options.agents as agent (agent.code)}
							<option value={agent.code}>{agent.name}</option>
						{/each}
					</select>
					{#if fieldErrors.agentCode}
						<span class="text-xs text-danger">{fieldErrors.agentCode}</span>
					{/if}
				</label>
			</div>
			<Field
				label={t(locale, 'workflows.field.description')}
				name="description"
				error={fieldErrors.description}
			/>
			<label class="flex items-center gap-2 text-sm">
				<input type="checkbox" name="enabled" checked />
				<span>{t(locale, 'workflows.field.enabled')}</span>
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
</section>
