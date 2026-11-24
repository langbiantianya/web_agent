<script lang="ts">
	interface Props {
		q: string;
		sort: string;
		page: number;
		total: number;
		size: number;
		sorts: { value: string; label: string }[];
		searchLabel: string;
		sortLabel: string;
		applyLabel: string;
		summary: string;
		prevLabel: string;
		nextLabel: string;
	}

	let {
		q,
		sort,
		page,
		total,
		size,
		sorts,
		searchLabel,
		sortLabel,
		applyLabel,
		summary,
		prevLabel,
		nextLabel
	}: Props = $props();

	const pages = $derived(Math.max(1, Math.ceil(total / Math.max(1, size))));

	/** 拼出保留当前搜索与排序、只改页码的查询串。 */
	function pageLink(target: number): string {
		const params = new URLSearchParams();
		if (q) params.set('q', q);
		if (sort) params.set('sort', sort);
		params.set('page', String(Math.max(1, Math.min(target, pages))));
		return `?${params.toString()}`;
	}
</script>

<form class="flex flex-wrap items-end gap-3" method="GET">
	<label class="flex flex-col gap-1 text-sm">
		<span class="font-medium">{searchLabel}</span>
		<input
			class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
			name="q"
			value={q}
		/>
	</label>
	<label class="flex flex-col gap-1 text-sm">
		<span class="font-medium">{sortLabel}</span>
		<select
			class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
			name="sort"
			value={sort}
		>
			{#each sorts as option (option.value)}
				<option value={option.value}>{option.label}</option>
			{/each}
		</select>
	</label>
	<button
		class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
		type="submit"
	>
		{applyLabel}
	</button>
	<span class="pb-2 text-sm text-ink-muted">{summary}</span>
	<span class="ml-auto flex items-center gap-2 pb-2 text-sm">
		<a
			class="rounded-card border border-line px-3 py-1.5"
			class:pointer-events-none={page <= 1}
			aria-disabled={page <= 1}
			href={pageLink(page - 1)}
		>
			{prevLabel}
		</a>
		<span class="text-ink-muted">{page} / {pages}</span>
		<a
			class="rounded-card border border-line px-3 py-1.5"
			class:pointer-events-none={page >= pages}
			aria-disabled={page >= pages}
			href={pageLink(page + 1)}
		>
			{nextLabel}
		</a>
	</span>
</form>