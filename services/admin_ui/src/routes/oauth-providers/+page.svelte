<script lang="ts">
	import { enhance } from '$app/forms';
	import Panel from '../../lib/components/Panel.svelte';
	import ProviderForm from './ProviderForm.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { PageProps } from './$types';

	let { data, form }: PageProps = $props();

	const locale = $derived(data.locale as Locale);
	const payload = $derived(data.payload);
	const editing = $derived(
		payload.items.find((item) => item.code === data.editing) ?? null
	);

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
</script>

<svelte:head>
	<title>{t(locale, 'oauth.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'oauth.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'oauth.description')}</p>
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
							<th class="pb-2 pr-4">{t(locale, 'common.code')}</th>
							<th class="pb-2 pr-4">{t(locale, 'oauth.column.issuer')}</th>
							<th class="pb-2 pr-4">{t(locale, 'oauth.column.loginScopes')}</th>
							<th class="pb-2 pr-4">{t(locale, 'oauth.column.roleSync')}</th>
							<th class="pb-2 pr-4">{t(locale, 'oauth.column.state')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.code)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4">
									<div class="font-mono text-xs">{item.code}</div>
									<div class="text-xs text-ink-muted">{item.displayName}</div>
								</td>
								<td class="py-2 pr-4 font-mono text-xs">{item.issuer}</td>
								<td class="py-2 pr-4 text-xs">{item.scopes.join(' ') || '—'}</td>
								<td class="py-2 pr-4 text-xs">
									{item.roleClaim}
									<span class="text-ink-muted">
										({item.roleSyncMaxAgeSeconds}s)
									</span>
								</td>
								<td class="py-2 pr-4 text-xs">
									<div>{item.enabled ? t(locale, 'common.enabled') : t(locale, 'common.disabled')}</div>
									<div class="text-ink-muted">
										{item.hasClientSecret
											? t(locale, 'oauth.secret.configured')
											: t(locale, 'oauth.secret.missing')}
									</div>
								</td>
								<td class="py-2 pr-4">
									<div class="flex items-center gap-2">
										<form method="POST" action="?/toggle" use:enhance>
											<input type="hidden" name="code" value={item.code} />
											<input type="hidden" name="enabled" value={item.enabled ? 'off' : 'on'} />
											<button class="rounded-card border border-line px-2 py-1 text-xs" type="submit">
												{item.enabled
													? t(locale, 'common.enabled')
													: t(locale, 'common.disabled')}
											</button>
										</form>
										<a
											class="rounded-card border border-line px-2 py-1 text-xs"
											href="?edit={encodeURIComponent(item.code)}"
										>
											{t(locale, 'common.edit')}
										</a>
										<form method="POST" action="?/delete" use:enhance>
											<input type="hidden" name="code" value={item.code} />
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

	{#if editing}
		<Panel title={t(locale, 'common.edit')}>
			<form class="flex flex-col gap-3" method="POST" action="?/update" use:enhance>
				<input type="hidden" name="code" value={editing.code} />
				<ProviderForm {locale} mode="edit" values={editing} errors={fieldErrors} />
				<div class="flex gap-2">
					<button
						class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink"
						type="submit"
					>
						{t(locale, 'common.save')}
					</button>
					<a
						class="rounded-card border border-line px-3 py-2 text-sm"
						href="/admin/oauth-providers"
					>
						{t(locale, 'common.cancel')}
					</a>
				</div>
			</form>
		</Panel>
	{:else}
		<Panel title={t(locale, 'common.create')}>
			<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
				<ProviderForm {locale} mode="create" errors={fieldErrors} />
				<div class="flex gap-2">
					<button
						class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink"
						type="submit"
					>
						{t(locale, 'common.create')}
					</button>
				</div>
			</form>
		</Panel>
	{/if}
</section>
