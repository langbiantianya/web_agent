<script lang="ts">
	import { enhance } from '$app/forms';
	import Field from '../../../lib/components/Field.svelte';
	import Panel from '../../../lib/components/Panel.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);

	let providerId = $state('');
	let roleCode = $state('');

	$effect(() => {
		if (!providerId && payload.providers.length > 0) {
			providerId = String(payload.providers[0].id);
		}
		if (!roleCode && payload.roleCodes.length > 0) {
			roleCode = payload.roleCodes[0];
		}
	});
</script>

<svelte:head>
	<title>{t(locale, 'access.mappings.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'access.mappings.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'access.mappings.description')}</p>
	</header>

	{#if form?.message}
		<p class="rounded-card border border-danger bg-surface px-3 py-2 text-sm text-danger">
			{form.message}
		</p>
	{/if}

	<Panel>
		{#if payload.items.length === 0}
			<p class="text-sm text-ink-muted">{t(locale, 'common.empty')}</p>
		{:else}
			<div class="overflow-x-auto">
				<table class="w-full min-w-[820px] text-left text-sm">
					<thead class="text-xs uppercase tracking-wider text-ink-muted">
						<tr>
							<th class="pb-2 pr-4">{t(locale, 'access.mappings.column.provider')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.mappings.column.claim')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.mappings.column.externalValue')}</th>
							<th class="pb-2 pr-4">{t(locale, 'access.mappings.column.role')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.status')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.id)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4">
									<span class="font-medium">{item.providerName}</span>
									<span class="block font-mono text-xs text-ink-muted">
										{item.providerCode}
									</span>
								</td>
								<td class="py-2 pr-4 font-mono text-xs">{item.claimName}</td>
								<td class="py-2 pr-4 font-mono text-xs">{item.externalValue}</td>
								<td class="py-2 pr-4 font-mono text-xs">{item.roleCode}</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">
									{item.enabled ? t(locale, 'common.enabled') : t(locale, 'common.disabled')}
								</td>
								<td class="py-2 pr-4">
									<div class="flex flex-wrap items-center gap-2">
										<form method="POST" action="?/update" use:enhance>
											<input type="hidden" name="mappingId" value={item.id} />
											<input type="hidden" name="roleCode" value={item.roleCode} />
											<input
												type="hidden"
												name="enabled"
												value={item.enabled ? 'off' : 'on'}
											/>
											<button class="rounded-card border border-line px-2 py-1 text-xs" type="submit">
												{item.enabled
													? t(locale, 'common.disabled')
													: t(locale, 'common.enabled')}
											</button>
										</form>
										<form method="POST" action="?/delete" use:enhance>
											<input type="hidden" name="mappingId" value={item.id} />
											<button
												class="rounded-card border border-danger px-2 py-1 text-xs text-danger"
												type="submit"
											>
												{t(locale, 'common.delete')}
											</button>
										</form>
									</div>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	</Panel>

	<Panel title={t(locale, 'common.create')}>
		<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
			<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'access.mappings.field.provider')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="providerId"
						bind:value={providerId}
					>
						{#each payload.providers as provider (provider.id)}
							<option value={String(provider.id)}>
								{provider.name} ({provider.code})
							</option>
						{/each}
					</select>
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'access.mappings.field.role')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="roleCode"
						bind:value={roleCode}
					>
						{#each payload.roleCodes as code (code)}
							<option value={code}>{code}</option>
						{/each}
					</select>
				</label>
			</div>
			<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
				<Field
					label={t(locale, 'access.mappings.field.claim')}
					name="claimName"
					value="groups"
					error={fieldErrors.claimName}
				/>
				<Field
					label={t(locale, 'access.mappings.field.externalValue')}
					name="externalValue"
					required
					error={fieldErrors.externalValue}
				/>
			</div>
			<div class="flex gap-2">
				<button class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink" type="submit">
					{t(locale, 'common.create')}
				</button>
			</div>
		</form>
	</Panel>
</section>
