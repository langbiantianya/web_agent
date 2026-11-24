<script lang="ts">
	import '../app.css';
	import favicon from '../lib/assets/favicon.svg';
	import Sidebar from '../lib/components/Sidebar.svelte';
	import Topbar from '../lib/components/Topbar.svelte';
	import { groupTitle, pageTitle, t, type Locale } from '#lib/i18n';
	import type { LayoutProps } from './$types';

	let { data, children }: LayoutProps = $props();

	const locale = $derived(data.locale as Locale);
	const session = $derived(data.session);

	$effect(() => {
		if (session) {
			document.documentElement.dataset.adminTheme = session.preferences.theme;
			document.documentElement.dataset.adminColorMode = session.preferences.colorMode;
			document.documentElement.dataset.adminDensity = session.preferences.density;
		}
	});
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>{t(locale, 'app.title')}</title>
</svelte:head>

<div class="flex min-h-screen">
	<Sidebar
		pages={session?.pages ?? []}
		{locale}
		titleFor={(code, fallback) => pageTitle(locale, code, fallback)}
		groupFor={(group, fallback) => groupTitle(locale, group, fallback)}
	/>
	<div class="flex min-w-0 flex-1 flex-col">
		<Topbar principal={session?.principal} {locale} />
		<main class="flex-1 p-6">
			{@render children()}
		</main>
	</div>
</div>

<style>
	:global(body) {
		margin: 0;
	}
</style>