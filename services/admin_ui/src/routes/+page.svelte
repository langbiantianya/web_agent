<script lang="ts">
	import { pythonPath } from '#lib/paths';
	import MetricCard from '../lib/components/MetricCard.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const counts = $derived(data.payload.counts);
	const components = $derived(data.payload.components);
</script>

<section class="flex flex-col gap-4">
	<h1 class="text-2xl font-semibold">{t(locale, 'dashboard.title')}</h1>

	{#if components.metrics}
		<section class="rounded-card bg-surface p-5 shadow-sm">
			<h2 class="mb-4 text-lg font-medium">{t(locale, 'dashboard.platformOverview')}</h2>
			<div class="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
				<MetricCard label={t(locale, 'dashboard.conversations')} value={counts.conversations} />
				<MetricCard label={t(locale, 'dashboard.runs')} value={counts.runs} />
				<MetricCard label={t(locale, 'dashboard.activeAdmins')} value={counts.admins} />
				<MetricCard label={t(locale, 'dashboard.platformAdmins')} value={counts.platformAdmins} />
			</div>
		</section>
	{/if}

	{#if components.quickActions}
		<section class="rounded-card bg-surface p-5 shadow-sm">
			<h2 class="mb-3 text-lg font-medium">{t(locale, 'dashboard.quickActions')}</h2>
			<div class="flex flex-wrap gap-3">
				<a class="rounded-card border border-line px-3 py-2 text-sm" href={pythonPath('/access/pages')}>
					{t(locale, 'dashboard.editPagePermissions')}
				</a>
				<a class="rounded-card border border-line px-3 py-2 text-sm" href={pythonPath('/access/components')}>
					{t(locale, 'dashboard.editComponentPermissions')}
				</a>
				<a class="rounded-card border border-line px-3 py-2 text-sm" href={pythonPath('/access/roles')}>
					{t(locale, 'dashboard.reviewCasbinRoles')}
				</a>
			</div>
		</section>
	{/if}

	{#if !components.metrics && !components.quickActions}
		<section class="rounded-card bg-surface p-5 text-sm text-ink-muted">
			{t(locale, 'dashboard.empty')}
		</section>
	{/if}
</section>