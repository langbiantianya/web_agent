<script lang="ts">
	interface Props {
		label: string;
		value: number;
	}

	let { label, value }: Props = $props();
</script>

<div class="rounded-card bg-surface-2 p-4">
	<div class="text-sm text-ink-muted">{label}</div>
	<div class="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
</div>