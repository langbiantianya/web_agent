<script lang="ts">
	import type { PermissionOption } from '#lib/types';

	interface Props {
		/** 权限目录。 */
		options: PermissionOption[];
		/** 已选中的权限值（``对象.操作``）。 */
		selected?: string[];
		/** 复选框的表单字段名。 */
		name?: string;
	}

	let { options, selected = $bindable<string[]>([]), name = 'permissions' }: Props = $props();

	/** 按对象分组渲染成矩阵。 */
	const groups = $derived.by(() => {
		const map = new Map<string, PermissionOption[]>();
		for (const option of options) {
			const list = map.get(option.object) ?? [];
			list.push(option);
			map.set(option.object, list);
		}
		return [...map.entries()];
	});
</script>

<div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
	{#each groups as [object, items] (object)}
		<fieldset class="rounded-card border border-line p-3">
			<legend class="px-1 font-mono text-xs uppercase tracking-wider text-ink-muted">
				{object}
			</legend>
			{#each items as option (option.id)}
				<label class="flex items-start gap-2 py-1 text-sm">
					<input
						type="checkbox"
						{name}
						value={option.id}
						bind:group={selected}
					/>
					<span class="min-w-0">
						<span class="font-mono text-xs">{option.id}</span>
						{#if option.description}
							<span class="block text-xs text-ink-muted">{option.description}</span>
						{/if}
					</span>
				</label>
			{/each}
		</fieldset>
	{/each}
</div>
