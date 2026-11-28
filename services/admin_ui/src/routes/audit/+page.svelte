<script lang="ts">
	import Field from '../../lib/components/Field.svelte';
	import Panel from '../../lib/components/Panel.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);
</script>

<svelte:head>
	<title>{t(locale, 'audit.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'audit.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'audit.description')}</p>
	</header>

	<Panel>
		<form class="flex flex-wrap items-end gap-3" method="GET">
			<Field label={t(locale, 'audit.field.runId')} name="runId" value={payload.filters.runId} />
			<Field label={t(locale, 'audit.field.toolName')} name="toolName" value={payload.filters.toolName} />
			<label class="flex flex-col gap-1 text-sm">
				<span class="font-medium">{t(locale, 'audit.field.source')}</span>
				<select
					class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
					name="source"
					value={payload.filters.source}
				>
					<option value="">—</option>
					{#each payload.sources as source (source)}
						<option value={source}>
							{t(locale, `audit.source.${source}` as 'audit.source.builtin')}
						</option>
					{/each}
				</select>
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span class="font-medium">{t(locale, 'audit.field.result')}</span>
				<select
					class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
					name="ok"
					value={payload.filters.ok}
				>
					<option value="">—</option>
					<option value="true">{t(locale, 'audit.result.ok')}</option>
					<option value="false">{t(locale, 'audit.result.failed')}</option>
				</select>
			</label>
			<button class="rounded-card border border-surface px-3 py-2 text-sm" type="submit">
				{t(locale, 'common.apply')}
			</button>
			<span class="pb-2 text-sm text-ink-muted">
				{t(locale, 'common.total', { count: payload.total })}
			</span>
		</form>
	</Panel>

	<Panel>
		{#if payload.items.length === 0}
			<p class="text-sm text-ink-muted">{t(locale, 'common.empty')}</p>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full min-w-[820px] text-left text-sm">
					<thead class="text-xs uppercase tracking-wider text-ink-muted">
						<tr>
							<th class="pb-2 pr-4">{t(locale, 'audit.column.tool')}</th>
							<th class="pb-2 pr-4">{t(locale, 'audit.column.source')}</th>
							<th class="pb-2 pr-4">{t(locale, 'audit.column.run')}</th>
							<th class="pb-2 pr-4">{t(locale, 'audit.column.result')}</th>
							<th class="pb-2 pr-4">{t(locale, 'audit.column.duration')}</th>
							<th class="pb-2 pr-4">{t(locale, 'audit.column.time')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.id)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4 font-mono text-xs">
									{item.toolName}
									{#if item.server}
										<span class="block text-ink-muted">{item.server}</span>
									{/if}
								</td>
								<td class="py-2 pr-4 text-xs">
									{t(locale, `audit.source.${item.source}` as 'audit.source.builtin')}
								</td>
								<td class="py-2 pr-4 font-mono text-xs text-ink-muted">{item.runId}</td>
								<td class="py-2 pr-4 text-xs">
									{item.ok
										? t(locale, 'audit.result.ok')
										: t(locale, 'audit.result.failed')}
									{#if item.failureType}
										<span class="block text-ink-muted">{item.failureType}</span>
									{/if}
								</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">{item.durationMs} ms</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">{item.createdAt ?? '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</Panel>
</section>
