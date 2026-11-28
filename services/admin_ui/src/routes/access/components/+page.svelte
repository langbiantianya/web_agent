<script lang="ts">
	import { enhance } from '$app/forms';
	import Panel from '../../../lib/components/Panel.svelte';
	import PermissionPicker from '../../../lib/components/PermissionPicker.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);
	const session = $derived(data.session);

	/** 被编辑的组件：``页面/组件``。 */
	const editingKey = $derived(data.editing);
	const editing = $derived(
		payload.items.find(
			(item) => `${item.pageCode}/${item.componentCode}` === editingKey
		) ?? null
	);

	let scope = $state('');
	let permissions = $state<string[]>([]);

	const isSuperAdmin = $derived(Boolean(session?.principal.isPlatformSuperAdmin));

	const scopeOptions = $derived.by(() => {
		const tenants = payload.tenantOptions.map((tenant) => ({
			value: tenant,
			label: tenant
		}));
		return isSuperAdmin
			? [{ value: '', label: t(locale, 'common.globalScope') }, ...tenants]
			: tenants;
	});

	/** 某范围内已绑定的权限。 */
	function bindingsOf(key: string, scopeValue: string): string[] {
		const item = payload.items.find(
			(entry) => `${entry.pageCode}/${entry.componentCode}` === key
		);
		const binding = item?.bindings.find(
			(entry) => (entry.tenantId ?? '') === scopeValue
		);
		return binding ? [...binding.permissions] : [];
	}

	function defaultScope(): string {
		if (isSuperAdmin) return '';
		return session?.principal.tenantId ?? '';
	}

	$effect(() => {
		const key = editingKey;
		const initial = defaultScope();
		scope = initial;
		permissions = key ? bindingsOf(key, initial) : [];
	});

	function onScopeChange(value: string): void {
		scope = value;
		permissions = editingKey ? bindingsOf(editingKey, value) : [];
	}

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);

	/** 按页面分组，方便定位。 */
	const byPage = $derived.by(() => {
		const map = new Map<string, typeof payload.items>();
		for (const item of payload.items) {
			const list = map.get(item.pageCode) ?? [];
			list.push(item);
			map.set(item.pageCode, list);
		}
		return [...map.entries()];
	});
</script>

<svelte:head>
	<title>{t(locale, 'access.components.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'access.components.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'access.components.description')}</p>
	</header>

	{#if form?.message}
		<p class="rounded-card border border-danger bg-surface px-3 py-2 text-sm text-danger">
			{form.message}
		</p>
	{/if}

	{#each byPage as [pageCode, items] (pageCode)}
		<Panel title={items[0]?.pageTitle ?? pageCode}>
			<div class="overflow-x-auto">
				<table class="w-full min-w-[720px] text-left text-sm">
					<thead class="text-xs uppercase tracking-wider text-ink-muted">
						<tr>
							<th class="pb-2 pr-4">{t(locale, 'access.components.column.component')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.components.column.kind')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.pages.column.scope')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.components.column.permissions')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each items as item (item.componentCode)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4">
									<span class="font-medium">{item.label}</span>
									<span class="block font-mono text-xs text-ink-muted">
										{item.componentCode}
									</span>
								</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">{item.kind}</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">
									{#each item.bindings as binding (String(binding.tenantId))}
										<span class="block">
											{binding.tenantId ?? t(locale, 'common.globalScope')}
										</span>
									{:else}
										<span>—</span>
									{/each}
								</td>
								<td class="py-2 pr-4">
									{#each item.bindings as binding (String(binding.tenantId))}
										<div class="mb-1 flex flex-wrap gap-1">
											{#each binding.permissions as permission (permission)}
												<span class="rounded-card border border-line px-2 py-0.5 font-mono text-xs">
													{permission}
												</span>
											{/each}
										</div>
									{/each}
								</td>
								<td class="py-2 pr-4">
									<a
										class="rounded-card border border-line px-2 py-1 text-xs"
										href="?edit={encodeURIComponent(`${item.pageCode}/${item.componentCode}`)}"
									>
										{t(locale, 'common.edit')}
									</a>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</Panel>
	{/each}

	{#if editing}
		<Panel title={`${editing.pageTitle} · ${editing.label}`}>
			<form class="flex flex-col gap-4" method="POST" action="?/save" use:enhance>
				<input type="hidden" name="pageCode" value={editing.pageCode} />
				<input type="hidden" name="componentCode" value={editing.componentCode} />
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'access.pages.column.scope')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="tenantId"
						value={scope}
						onchange={(event) => onScopeChange(event.currentTarget.value)}
					>
						{#each scopeOptions as option (option.value)}
							<option value={option.value}>{option.label}</option>
						{/each}
					</select>
				</label>
				<PermissionPicker options={payload.permissionOptions} bind:selected={permissions} />
				{#if fieldErrors.permissions}
					<p class="text-xs text-danger">{fieldErrors.permissions}</p>
				{/if}
				<div class="flex gap-2">
					<button class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink" type="submit">
						{t(locale, 'common.save')}
					</button>
					<button
						class="rounded-card border border-line px-3 py-2 text-sm"
						type="submit"
						formaction="?/reset"
					>
						{t(locale, 'common.reset')}
					</button>
					<a class="rounded-card border border-line px-3 py-2 text-sm" href="/admin/access/components">
						{t(locale, 'common.cancel')}
					</a>
				</div>
			</form>
		</Panel>
	{/if}
</section>
