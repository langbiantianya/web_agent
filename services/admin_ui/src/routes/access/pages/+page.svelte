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
	const editing = $derived(
		payload.items.find((item) => item.code === data.editing) ?? null
	);

	/** 当前选中的生效范围；空串代表全局。 */
	let scope = $state('');
	/** 当前选中的权限集合。 */
	let permissions = $state<string[]>([]);

	const isSuperAdmin = $derived(Boolean(session?.principal.isPlatformSuperAdmin));

	/** 可选的生效范围：平台超级管理员可以写全局与各租户。 */
	const scopeOptions = $derived.by(() => {
		const tenants = payload.tenantOptions.map((tenant) => ({
			value: tenant,
			label: tenant
		}));
		return isSuperAdmin ? [{ value: '', label: t(locale, 'common.globalScope') }, ...tenants] : tenants;
	});

	/** 某范围内已绑定的权限。 */
	function bindingsOf(code: string, scopeValue: string): string[] {
		const item = payload.items.find((entry) => entry.code === code);
		const binding = item?.bindings.find(
			(entry) => (entry.tenantId ?? '') === scopeValue
		);
		return binding ? [...binding.permissions] : [];
	}

	/** 默认范围：平台超级管理员落在全局，其余落在自己的租户。 */
	function defaultScope(): string {
		if (isSuperAdmin) return '';
		return session?.principal.tenantId ?? '';
	}

	$effect(() => {
		const code = data.editing;
		const initial = defaultScope();
		scope = initial;
		permissions = code ? bindingsOf(code, initial) : [];
	});

	/** 切换生效范围时，换成那个范围上的绑定。 */
	function onScopeChange(value: string): void {
		scope = value;
		permissions = editing ? bindingsOf(editing.code, value) : [];
	}

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
</script>

<svelte:head>
	<title>{t(locale, 'access.pages.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'access.pages.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'access.pages.description')}</p>
	</header>

	{#if form?.message}
		<p class="rounded-card border border-danger bg-surface px-3 py-2 text-sm text-danger">
			{form.message}
		</p>
	{/if}

	<Panel>
		<div class="overflow-x-auto">
			<table class="w-full min-w-[720px] text-left text-sm">
				<thead class="text-xs uppercase tracking-wider text-ink-muted">
					<tr>
						<th class="pb-2 pr-4">{t(locale, 'common.name')}</th>
						<th class="pb-2 pr-4">{t(locale, 'access.pages.column.scope')}</th>
						<th class="pb-2 pr-4">{t(locale, 'access.pages.column.permissions')}</th>
						<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
					</tr>
				</thead>
				<tbody>
					{#each payload.items as item (item.code)}
						<tr class="border-t border-line">
							<td class="py-2 pr-4">
								<span class="font-medium">{item.title}</span>
								<span class="block font-mono text-xs text-ink-muted">{item.code}</span>
								{#if item.critical}
									<span class="mt-1 inline-block rounded-card bg-surface px-2 py-0.5 text-xs text-ink-muted">
										{t(locale, 'access.pages.critical')}
									</span>
								{/if}
							</td>
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
									href="?edit={encodeURIComponent(item.code)}"
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

	{#if editing}
		<Panel title={editing.title}>
			<form class="flex flex-col gap-4" method="POST" action="?/save" use:enhance>
				<input type="hidden" name="pageCode" value={editing.code} />
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
					<a class="rounded-card border border-line px-3 py-2 text-sm" href="/admin/access/pages">
						{t(locale, 'common.cancel')}
					</a>
				</div>
			</form>
		</Panel>
	{/if}
</section>
