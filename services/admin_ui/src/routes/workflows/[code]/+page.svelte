<script lang="ts">
	import { appPath } from '#lib/paths';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const workflow = $derived(data.workflow);
</script>

<svelte:head>
	<title>{workflow.name} · {t(locale, 'workflows.canvas.title')}</title>
</svelte:head>

<section class="flex flex-col gap-3">
	<header class="flex flex-wrap items-center justify-between gap-3">
		<div class="flex flex-wrap items-baseline gap-3">
			<a class="text-sm text-ink-muted hover:underline" href={appPath('/workflows')}>
				← {t(locale, 'common.back')}
			</a>
			<h1 class="text-2xl font-semibold">{workflow.name}</h1>
			<span class="font-mono text-xs text-ink-muted">{workflow.code}</span>
		</div>
		<span class="rounded-card border border-line px-2 py-1 text-xs text-ink-muted">
			{workflow.enabled ? t(locale, 'common.enabled') : t(locale, 'common.disabled')}
		</span>
	</header>

	<!--
		画布本体。src 指向 Python 的片段页：浏览器始终只跟 SvelteKit 说话，
		画布要用的校验与保存请求由网关转发给 Python。
	-->
	<iframe
		src={data.canvasUrl}
		title={workflow.name}
		class="h-[calc(100vh-14rem)] min-h-[32rem] w-full rounded-card border border-line bg-surface"
	></iframe>
</section>
