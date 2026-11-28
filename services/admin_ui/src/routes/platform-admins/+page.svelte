<script lang="ts">
	import { enhance } from '$app/forms';
	import Field from '../../lib/components/Field.svelte';
	import Panel from '../../lib/components/Panel.svelte';
	import Toolbar from '../../lib/components/Toolbar.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
</script>

<svelte:head>
	<title>{t(locale, 'platformAdmins.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'platformAdmins.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'platformAdmins.description')}</p>
	</header>

	{#if form?.message}
		<p class="rounded-card border border-danger bg-surface px-3 py-2 text-sm text-danger">
			{form.message}
		</p>
	{/if}

	{#if data.undoId}
		<form
			class="flex items-center gap-3 rounded-card border border-line bg-surface px-3 py-2 text-sm"
			method="POST"
			action="?/undo"
			use:enhance
		>
			<input type="hidden" name="undoId" value={data.undoId} />
			<span class="text-ink-muted">{t(locale, 'common.undoHint')}</span>
			<button class="rounded-card border border-line px-2 py-1 text-xs" type="submit">
				{t(locale, 'common.undo')}
			</button>
		</form>
	{/if}

	<Panel>
		<Toolbar
			q={payload.page.q}
			sort={payload.page.sort}
			page={payload.page.page}
			total={payload.total}
			size={payload.page.size}
			sorts={[{ value: '', label: t(locale, 'common.sort') }]}
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
				<table class="w-full min-w-[720px] text-left text-sm">
					<thead class="text-xs uppercase tracking-wider text-ink-muted">
						<tr>
							<th class="pb-2 pr-4">{t(locale, 'platformAdmins.column.username')}</th>
							<th class="pb-2 pr-4">{t(locale, 'platformAdmins.column.contact')}</th>
							<th class="pb-2 pr-4">{t(locale, 'platformAdmins.column.status')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.created')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.id)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4 font-mono text-xs">
									{item.username}
									{#if item.ownerSlot}
										<span class="ml-2 rounded-card bg-surface px-2 py-0.5 text-xs text-ink-muted">
											owner
										</span>
									{/if}
								</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">
									{item.email ?? item.phone ?? '—'}
								</td>
								<td class="py-2 pr-4 text-xs">{item.status}</td>
								<td class="py-2 pr-4 text-xs text-ink-muted">{item.createdAt ?? '—'}</td>
								<td class="py-2 pr-4">
									<div class="flex flex-wrap items-center gap-2">
										<form method="POST" action="?/reset" use:enhance>
											<input type="hidden" name="accountId" value={item.id} />
											<button class="rounded-card border border-line px-2 py-1 text-xs" type="submit">
												{t(locale, 'common.resetPassword')}
											</button>
										</form>
										{#if !item.ownerSlot}
											<form method="POST" action="?/delete" use:enhance>
												<input type="hidden" name="accountId" value={item.id} />
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

	<Panel title={t(locale, 'platformAdmins.create')}>
		<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
			<div class="grid grid-cols-1 gap-3 md:grid-cols-3">
				<Field
					label={t(locale, 'platformAdmins.field.username')}
					name="username"
					required
					error={fieldErrors.username}
				/>
				<Field
					label={t(locale, 'platformAdmins.field.email')}
					name="email"
					type="email"
					error={fieldErrors.email}
				/>
				<Field
					label={t(locale, 'platformAdmins.field.phone')}
					name="phone"
					error={fieldErrors.phone}
				/>
			</div>
			<div class="flex gap-2">
				<button class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink" type="submit">
					{t(locale, 'platformAdmins.create')}
				</button>
			</div>
		</form>
	</Panel>
</section>
