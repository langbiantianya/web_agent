<script lang="ts">
	import type { Snippet } from 'svelte';

	interface Props {
		label: string;
		name: string;
		value?: string;
		type?: string;
		required?: boolean;
		placeholder?: string;
		hint?: string;
		error?: string;
		textarea?: boolean;
		rows?: number;
		children?: Snippet;
	}

	let {
		label,
		name,
		value = $bindable(''),
		type = 'text',
		required = false,
		placeholder,
		hint,
		error,
		textarea = false,
		rows = 4,
		children
	}: Props = $props();
</script>

<label class="flex flex-col gap-1 text-sm">
	<span class="font-medium">{label}{#if required}<span class="text-danger"> *</span>{/if}</span>
	{#if textarea}
		<textarea
			class="rounded-card border border-line bg-surface px-3 py-2 font-mono text-sm"
			class:border-danger={Boolean(error)}
			{name}
			bind:value
			{rows}
			{placeholder}
		></textarea>
	{:else}
		<input
			class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
			class:border-danger={Boolean(error)}
			{name}
			{type}
			{required}
			{placeholder}
			bind:value
		/>
	{/if}
	{#if children}
		{@render children()}
	{:else if hint}
		<span class="text-xs text-ink-muted">{hint}</span>
	{/if}
	{#if error}
		<span class="text-xs text-danger">{error}</span>
	{/if}
</label>