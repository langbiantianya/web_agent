<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		title?: string;
		actions?: Snippet;
		children: Snippet;
	}

	let { title, actions, children }: Props = $props();
</script>

<section class="rounded-card bg-surface p-5 shadow-sm">
	{#if title || actions}
		<header class="mb-4 flex items-center justify-between gap-3">
			{#if title}
				<h2 class="text-lg font-medium">{title}</h2>
			{/if}
			{#if actions}
				{@render actions()}
			{/if}
		</header>
	{/if}
	{@render children()}
</section>