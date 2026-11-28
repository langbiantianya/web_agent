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
	const editing = $derived(
		payload.items.find((item) => item.code === data.editing) ?? null
	);

	const fieldErrors = $derived((form?.fieldErrors ?? {}) as Record<string, string>);
	const sorts = $derived([
		{ value: '', label: t(locale, 'agents.sort.default') },
		{ value: 'name', label: t(locale, 'agents.sort.name') },
		{ value: 'code', label: t(locale, 'agents.sort.code') },
		{ value: 'created', label: t(locale, 'agents.sort.created') }
	]);
</script>

<svelte:head>
	<title>{t(locale, 'agents.title')} · web_agent</title>
</svelte:head>

<section class="flex flex-col gap-4">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">{t(locale, 'agents.title')}</h1>
		<p class="text-sm text-ink-muted">{t(locale, 'agents.description')}</p>
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
			{sorts}
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
							<th class="pb-2 pr-4">{t(locale, 'common.code')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.name')}</th>
							<th class="pb-2 pr-4">{t(locale, 'agents.column.endpoint')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.model')}</th>
							<th class="pb-2 pr-4">{t(locale, 'agents.column.response')}</th>
							<th class="pb-2 pr-4">{t(locale, 'common.actions')}</th>
						</tr>
					</thead>
					<tbody>
						{#each payload.items as item (item.code)}
							<tr class="border-t border-line">
								<td class="py-2 pr-4 font-mono text-xs">{item.code}</td>
								<td class="py-2 pr-4">{item.name}</td>
								<td class="py-2 pr-4">{item.llmEndpointCode || '—'}</td>
								<td class="py-2 pr-4">{item.model || '—'}</td>
								<td class="py-2 pr-4">
									{t(locale, `agents.responseFormat.${item.responseFormat}` as 'agents.responseFormat.text')}
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
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<Field
						label={t(locale, 'agents.field.name')}
						name="name"
						value={editing.name}
						required
						error={fieldErrors.name}
					/>
					<Field
						label={t(locale, 'agents.field.model')}
						name="model"
						value={editing.model}
						error={fieldErrors.model}
					/>
				</div>
				<Field
					label={t(locale, 'agents.field.description')}
					name="description"
					value={editing.description}
					error={fieldErrors.description}
				/>
				<Field
					label={t(locale, 'agents.field.systemPrompt')}
					name="systemPrompt"
					value={editing.systemPrompt}
					textarea
					rows={6}
					required
					error={fieldErrors.systemPrompt}
				/>
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'agents.field.endpoint')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="llmEndpointCode"
					>
						{#each payload.options.llmEndpoints as endpoint (endpoint.code)}
							<option
								value={endpoint.code}
								selected={endpoint.code === editing.llmEndpointCode}
							>
								{endpoint.name}（{endpoint.model}）
							</option>
						{/each}
					</select>
					{#if fieldErrors.llm_endpoint_code}
						<span class="text-xs text-danger">{fieldErrors.llm_endpoint_code}</span>
					{/if}
				</label>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<label class="flex flex-col gap-1 text-sm">
						<span class="font-medium">{t(locale, 'agents.field.skills')}</span>
						<select
							class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
							name="skills"
							multiple
							size={6}
						>
							{#each payload.options.skills as skill (skill.code)}
								<option value={skill.code} selected={editing.skills.includes(skill.code)}>
									{skill.name}
								</option>
							{/each}
						</select>
						{#if fieldErrors.skills}
							<span class="text-xs text-danger">{fieldErrors.skills}</span>
						{/if}
					</label>
					<label class="flex flex-col gap-1 text-sm">
						<span class="font-medium">{t(locale, 'agents.field.tools')}</span>
						<select
							class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
							name="tools"
							multiple
							size={6}
						>
							{#each payload.options.mcpTools as tool (tool)}
								<option value={tool} selected={editing.tools.includes(tool)}>{tool}</option>
							{/each}
						</select>
						{#if fieldErrors.tools}
							<span class="text-xs text-danger">{fieldErrors.tools}</span>
						{/if}
					</label>
				</div>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
					<label class="flex flex-col gap-1 text-sm">
						<span class="font-medium">{t(locale, 'agents.field.responseFormat')}</span>
						<select
							class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
							name="responseFormat"
						>
							<option value="text" selected={editing.responseFormat === 'text'}>
								{t(locale, 'agents.responseFormat.text')}
							</option>
							<option value="json" selected={editing.responseFormat === 'json'}>
								{t(locale, 'agents.responseFormat.json')}
							</option>
						</select>
						{#if fieldErrors.response_format}
							<span class="text-xs text-danger">{fieldErrors.response_format}</span>
						{/if}
					</label>
					<Field
						label={t(locale, 'agents.field.responseSchema')}
						name="responseSchema"
						value={editing.responseSchema ? JSON.stringify(editing.responseSchema, null, 2) : ''}
						textarea
						rows={4}
						hint={t(locale, 'agents.responseSchema.hint')}
						error={fieldErrors.response_schema}
					/>
				</div>
				<label class="flex items-center gap-2 text-sm">
					<input type="checkbox" name="enabled" checked={editing.enabled} />
					<span>{t(locale, 'agents.field.enabled')}</span>
				</label>
				<div class="flex gap-2">
					<button
						class="rounded-card bg-accent px-3 py-2 text-sm text-accent-ink"
						type="submit"
					>
						{t(locale, 'common.save')}
					</button>
					<a class="rounded-card border border-line px-3 py-2 text-sm" href="/admin/agents">
						{t(locale, 'common.cancel')}
					</a>
				</div>
			</form>
		</Panel>
	{:else}
		<Panel title={t(locale, 'common.create')}>
			<form class="flex flex-col gap-3" method="POST" action="?/create" use:enhance>
				<div class="grid grid-cols-1 gap-3 md:grid-cols-3">
					<Field
						label={t(locale, 'agents.field.code')}
						name="code"
						required
						error={fieldErrors.code}
					/>
					<Field
						label={t(locale, 'agents.field.name')}
						name="name"
						required
						error={fieldErrors.name}
					/>
					<Field
						label={t(locale, 'agents.field.model')}
						name="model"
						error={fieldErrors.model}
					/>
				</div>
				<Field
					label={t(locale, 'agents.field.description')}
					name="description"
					error={fieldErrors.description}
				/>
				<Field
					label={t(locale, 'agents.field.systemPrompt')}
					name="systemPrompt"
					textarea
					rows={6}
					required
					error={fieldErrors.systemPrompt}
				/>
				<label class="flex flex-col gap-1 text-sm">
					<span class="font-medium">{t(locale, 'agents.field.endpoint')}</span>
					<select
						class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
						name="llmEndpointCode"
					>
						{#each payload.options.llmEndpoints as endpoint (endpoint.code)}
							<option value={endpoint.code}>{endpoint.name}（{endpoint.model}）</option>
						{/each}
					</select>
					{#if fieldErrors.llm_endpoint_code}
						<span class="text-xs text-danger">{fieldErrors.llm_endpoint_code}</span>
					{/if}
				</label>
				<label class="flex items-center gap-2 text-sm">
					<input type="checkbox" name="enabled" checked />
					<span>{t(locale, 'agents.field.enabled')}</span>
				</label>
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