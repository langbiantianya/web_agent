<script lang="ts">
	import { enhance } from '$app/forms';
	import Field from '../../../lib/components/Field.svelte';
	import Panel from '../../../lib/components/Panel.svelte';
	import PermissionPicker from '../../../lib/components/PermissionPicker.svelte';
	import Toolbar from '../../../lib/components/Toolbar.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);
	const session = $derived(data.session);

	const isSuperAdmin = $derived(Boolean(session?.principal.isPlatformSuperAdmin));

	const editing = $derived(
		payload.items.find(
			(item) => item.code === data.editing && item.domain === data.editingDomain
		) ?? null
	);

	/** 可选域名：平台超级管理员能写任意域，其余管理员只有本租户域。 */
	const domainOptions = $derived.by(() => {
		const known = payload.domains;
		if (!isSuperAdmin) {
			return [`tenant:${session?.principal.tenantId ?? ''}`];
		}
		const extra = ['tenant:*', '*'].filter((domain) => !known.includes(domain));
		return [...known, ...extra];
	});

	/** 父角色候选：同域名下的其它角色。 */
	const parentCandidates = $derived(
		(payload.rolesByDomain[editing?.domain ?? ''] ?? []).filter(
			(code) => code !== editing?.code
		)
	);

	let permissions = $state<string[]>([]);
	let parents = $state<string[]>([]);
	let createPermissions = $state<string[]>([]);
	let createDomain = $state('');

	$effect(() => {
		permissions = editing ? [...editing.permissions] : [];
		parents = editing ? [...editing.parents] : [];
	});

	$effect(() => {
		if (!createDomain && domainOptions.length > 0) createDomain = domainOptions[0];
	});

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
</script>

<svelte:head>
	<title>{t(locale, 'access.roles.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'access.roles.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'access.roles.description')}</p>
	</header>

	{#if form?.message}
		<p class="rounded-card border border-danger bg-surface px-3 py-2 text-sm text-danger">
			{form.message}
		</p>
	{/if}

	<Panel>
		<Toolbar
			q={payload.page.q}
			sort={payload.page.sort}
			page={payload.page.page}
			total={payload.total}
			size={payload.page.size}
			sorts={[
				{ value: '', label: t(locale, 'access.roles.sort.default') },
				{ value: 'code', label: t(locale, 'access.roles.sort.code') },
				{ value: 'domain', label: t(locale, 'access.roles.sort.domain') }
			]}
			searchLabel={t(locale, 'common.search')}
			sortLabel={t(locale, 'common.sort')}
			applyLabel={t(locale, 'common.apply')}
			summary={t(locale, 'common.total', { count: payload.total })}
			prevLabel={t(locale, 'common.prev')}
			nextLabel={t(locale, 'common.next')}
		/>
	</Panel>

	<Panel>
		{#if payload.items.length === 0}
			<p class="text-sm text-ink-muted">{t(locale, 'common.empty')}</p>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full min-w-[860px] text-left text-sm">
					<thead class="text-xs uppercase tracking-wider text-ink-muted">
						<tr>
							<th class="pb-2 pr-4">{t(locale, 'common.code')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.domain')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.roles.column.permissions')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.roles.column.parents')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.roles.column.references')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.code + '@' + item.domain)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4 font-mono text-xs">
									{item.code}
									{#if item.builtin}
										<span class="ml-2 rounded-card bg-surface px-2 py-0.5 text-xs text-ink-muted">
											{t(locale, 'access.roles.builtin')}
										</span>
									{/if}
								</td>
								<td class="py-2 pr-4 font-mono text-xs text-ink-muted">{item.domain}</td>
								<td class="py-2 pr-4">
									<div class="flex flex-wrap gap-1">
										{#each item.permissions as permission (permission)}
											<span class="rounded-card border border-line px-2 py-0.5 font-mono text-xs">
												{permission}
											</span>
										{/each}
									</div>
								</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">
									{item.parents.join(', ') || '—'}
								</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">
									{t(locale, 'common.members')} {item.memberCount} ·
									{t(locale, 'common.references')} {item.mappingCount}
								</td>
								<td class="py-2 pr-4">
									<div class="flex flex-wrap items-center gap-2">
										<a
											class="rounded-card border border-line px-2 py-1 text-xs"
											href="?edit={encodeURIComponent(item.code)}&domain={encodeURIComponent(item.domain)}"
										>
											{t(locale, 'common.edit')}
										</a>
										{#if item.canDelete}
											<form method="POST" action="?/delete" use:enhance>
												<input type="hidden" name="roleCode" value={item.code} />
												<input type="hidden" name="domain" value={item.domain} />
												<button
													class="rounded-card border border-danger px-2 py-1 text-xs text-danger"
													type="submit"
												>
													{t(locale, 'common.delete')}
												</button>
											</form>
										{/if}
									</div>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</Panel>

	{#if editing}
		<Panel title={`${t(locale, 'access.roles.edit')} · ${editing.code}`}>
			<div class="flex flex-col gap-4">
				<form class="flex flex-col gap-3" method="POST" action="?/update" use:enhance>
					<input type="hidden" name="roleCode" value={editing.code} />
					<input type="hidden" name="domain" value={editing.domain} />
					<h3 class="text-sm font-medium">{t(locale, 'access.roles.field.permissions')}</h3>
					<PermissionPicker options={payload.permissionOptions} bind:selected={permissions} />
					{#if fieldErrors.permissions}
						<p class="text-xs text-danger">{fieldErrors.permissions}</p>
					{/if}
					<div class="flex gap-2">
						<button class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink" type="submit">
							{t(locale, 'common.save')}
						</button>
					</div>
				</form>

				<form class="flex flex-col gap-3" method="POST" action="?/inheritance" use:enhance>
					<input type="hidden" name="roleCode" value={editing.code} />
					<input type="hidden" name="domain" value={editing.domain} />
					<h3 class="text-sm font-medium">{t(locale, 'access.roles.inheritance')}</h3>
					<div class="flex flex-wrap gap-2">
						{#each parentCandidates as candidate (candidate)}
							<label class="flex items-center gap-2 rounded-card border border-line px-2 py-1 text-sm">
								<input type="checkbox" name="parents" value={candidate} bind:group={parents} />
								<span class="font-mono text-xs">{candidate}</span>
							</label>
						{:else}
							<p class="text-sm text-ink-muted">{t(locale, 'common.empty')}</p>
						{/each}
					</div>
					<div class="flex gap-2">
						<button class="rounded-card border border-line px-3 py-2 text-sm" type="submit">
							{t(locale, 'common.save')}
						</button>
					</div>
				</form>

				<div class="flex gap-2">
					<a class="rounded-card border border-line px-3 py-2 text-sm" href="/admin/access/roles">
						{t(locale, 'common.cancel')}
					</a>
				</div>
			</div>
		</Panel>
	{:else}
		<Panel title={t(locale, 'access.roles.create')}>
			<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<Field
						label={t(locale, 'access.roles.field.code')}
						name="code"
						required
						error={fieldErrors.code}
					/>
					<label class="flex flex-col gap-1 text-sm">
						<span class="font-medium">{t(locale, 'access.roles.field.domain')}</span>
						<select
							class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
							name="domain"
							bind:value={createDomain}
						>
							{#each domainOptions as domain (domain)}
								<option value={domain}>{domain}</option>
							{/each}
						</select>
					</label>
				</div>
				<PermissionPicker options={payload.permissionOptions} bind:selected={createPermissions} />
				{#if fieldErrors.permissions}
					<p class="text-xs text-danger">{fieldErrors.permissions}</p>
				{/if}
				<div class="flex gap-2">
					<button class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink" type="submit">
						{t(locale, 'common.create')}
					</button>
				</div>
			</form>
		</Panel>
	{/if}
</section>
