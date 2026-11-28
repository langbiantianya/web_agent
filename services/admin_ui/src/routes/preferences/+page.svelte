<script lang="ts">
	import { enhance } from '$app/forms';
	import Panel from '../../lib/components/Panel.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);

	let theme = $state('');
	let colorMode = $state('');
	let density = $state('');

	$effect(() => {
		theme = payload.theme;
		colorMode = payload.colorMode;
		density = payload.density;
	});

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
</script>

<svelte:head>
	<title>{t(locale, 'preferences.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'preferences.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'preferences.description')}</p>
	</header>

	{#if form?.message}
		<p class="rounded-card border border-danger bg-surface px-3 py-2 text-sm text-danger">
			{form.message}
		</p>
	{/if}

	<Panel>
		<form class="flex flex-col gap-3" method="POST" action="?/save" use:enhance>
			<div class="grid grid-cols-1 gap-3 md:grid-cols-3">
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'preferences.field.theme')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="theme"
						bind:value={theme}
					>
						{#each payload.options.themes as option (option)}
							<option value={option}>
								{t(locale, `preferences.theme.${option}` as 'preferences.theme.fluent')}
							</option>
						{/each}
					</select>
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'preferences.field.colorMode')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="colorMode"
						bind:value={colorMode}
					>
						{#each payload.options.colorModes as option (option)}
							<option value={option}>
								{t(locale, `preferences.colorMode.${option}` as 'preferences.colorMode.light')}
							</option>
						{/each}
					</select>
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'preferences.field.density')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="density"
						bind:value={density}
					>
						{#each payload.options.densities as option (option)}
							<option value={option}>
								{t(locale, `preferences.density.${option}` as 'preferences.density.compact')}
							</option>
						{/each}
					</select>
				</label>
			</div>
			{#if fieldErrors.theme || fieldErrors.colorMode || fieldErrors.density}
				<p class="text-xs text-danger">
					{fieldErrors.theme ?? fieldErrors.colorMode ?? fieldErrors.density}
				</p>
			{/if}
			<div class="flex gap-2">
				<button class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink" type="submit">
					{t(locale, 'common.save')}
				</button>
			</div>
		</form>
	</Panel>
</section>
