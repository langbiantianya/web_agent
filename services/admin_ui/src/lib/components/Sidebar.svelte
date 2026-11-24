<script lang="ts">
	import { page } from '$app/state';
	import type { AdminPage } from '#lib/types';
	import type { Locale } from '#lib/i18n';

	interface Props {
		pages: AdminPage[];
		locale: Locale;
		titleFor: (code: string, fallback: string) => string;
		groupFor: (group: string, fallback: string) => string;
	}

	let { pages, titleFor, groupFor }: Props = $props();

	const groups = $derived.by(() => {
		const order = ['Overview', 'Access control', 'Resources', 'Security', 'Settings', 'Operations'];
		const buckets = new Map<string, AdminPage[]>();
		for (const item of [...pages].sort((a, b) => a.navOrder - b.navOrder)) {
			const list = buckets.get(item.navGroup) ?? [];
			list.push(item);
			buckets.set(item.navGroup, list);
		}
		return order
			.filter((name) => buckets.has(name))
			.map((name) => ({ name, items: buckets.get(name) ?? [] }));
	});
</script>

<aside class="w-64 shrink-0 border-r border-line bg-surface">
	<div class="px-5 py-4 text-sm font-semibold tracking-wide text-ink-muted">web_agent</div>
	<nav class="flex flex-col gap-4 px-3 pb-6">
		{#each groups as group (group.name)}
			<div>
				<div class="px-2 pb-1 text-xs font-semibold uppercase tracking-wider text-ink-muted">
					{groupFor(group.name, group.name)}
				</div>
				<ul class="flex flex-col">
					{#each group.items as item (item.code)}
						<li>
							<a
								class="block rounded-card px-3 py-2 text-sm hover:bg-surface-2"
								class:bg-surface-2={page.url.pathname === item.routeKey}
								href={item.routeKey}
							>
								{titleFor(item.code, item.title)}
							</a>
						</li>
					{/each}
				</ul>
			</div>
		{/each}
	</nav>
</aside>