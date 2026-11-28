<script lang="ts">
	import Field from '../../lib/components/Field.svelte';
	import { t, type Locale } from '#lib/i18n';
	import type { AdminOAuthProvider } from '#lib/types';

	interface Props {
		locale: Locale;
		mode: 'create' | 'edit';
		values?: Partial<AdminOAuthProvider> | null;
		errors?: Record<string, string>;
	}

	let { locale, mode, values = null, errors = {} }: Props = $props();

	// 表单里的值走 camelCase，与管理面 API 的字段名一致——
	// 中间少一次翻译，就少一处「前端字段名和后端对不上」的坑。
	const v = $derived(values ?? {});
	const secretError = $derived(errors.clientSecret);
</script>

<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
	{#if mode === 'create'}
		<Field
			label={t(locale, 'oauth.field.code')}
			name="code"
			required
			error={errors.code}
		/>
	{/if}
	<Field
		label={t(locale, 'oauth.field.displayName')}
		name="displayName"
		required
		value={v.displayName ?? ''}
		error={errors.displayName}
	/>
</div>

<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
	<Field
		label={t(locale, 'oauth.field.issuer')}
		name="issuer"
		required
		value={v.issuer ?? ''}
		error={errors.issuer}
	/>
	<Field
		label={t(locale, 'oauth.field.clientId')}
		name="clientId"
		required
		value={v.clientId ?? ''}
		error={errors.clientId}
	/>
</div>

<Field
	label={t(locale, 'oauth.field.clientSecret')}
	name="clientSecret"
	type="password"
	hint={t(locale, 'oauth.secret.hint')}
	error={secretError}
/>

{#if mode === 'edit' && v.hasClientSecret}
	<label class="flex items-center gap-2 text-sm">
		<input type="checkbox" name="clearSecret" />
		<span>{t(locale, 'oauth.secret.clear')}</span>
	</label>
{/if}

<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
	<Field
		label={t(locale, 'oauth.field.discoveryUrl')}
		name="discoveryUrl"
		value={v.discoveryUrl ?? ''}
		error={errors.discoveryUrl}
	/>
	<Field
		label={t(locale, 'oauth.field.authorizationEndpoint')}
		name="authorizationEndpoint"
		value={v.authorizationEndpoint ?? ''}
		error={errors.authorizationEndpoint}
	/>
</div>

<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
	<Field
		label={t(locale, 'oauth.field.tokenEndpoint')}
		name="tokenEndpoint"
		value={v.tokenEndpoint ?? ''}
		error={errors.tokenEndpoint}
	/>
	<Field
		label={t(locale, 'oauth.field.jwksUri')}
		name="jwksUri"
		value={v.jwksUri ?? ''}
		error={errors.jwksUri}
	/>
</div>

<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
	<label class="flex flex-col gap-1 text-sm">
		<span class="font-medium">{t(locale, 'oauth.field.tokenAuthMethod')}</span>
		<select
			class="rounded-card border border-line bg-surface px-3 py-2 text-sm"
			name="tokenAuthMethod"
		>
			<option
				value="client_secret_basic"
				selected={(v.tokenAuthMethod ?? 'client_secret_basic') === 'client_secret_basic'}
			>
				{t(locale, 'oauth.tokenAuth.basic')}
			</option>
			<option
				value="client_secret_post"
				selected={v.tokenAuthMethod === 'client_secret_post'}
			>
				{t(locale, 'oauth.tokenAuth.post')}
			</option>
		</select>
	</label>
	<Field
		label={t(locale, 'oauth.field.scopes')}
		name="scopes"
		value={(v.scopes ?? []).join(' ')}
		error={errors.scopes}
	/>
</div>

<div class="grid grid-cols-1 gap-3 md:grid-cols-2">
	<Field
		label={t(locale, 'oauth.field.usernameClaim')}
		name="usernameClaim"
		value={v.usernameClaim ?? 'preferred_username'}
		error={errors.usernameClaim}
	/>
	<Field
		label={t(locale, 'oauth.field.roleClaim')}
		name="roleClaim"
		value={v.roleClaim ?? 'groups'}
		error={errors.roleClaim}
	/>
</div>

<Field
	label={t(locale, 'oauth.field.roleSyncMaxAge')}
	name="roleSyncMaxAgeSeconds"
	type="number"
	value={String(v.roleSyncMaxAgeSeconds ?? 1800)}
	error={errors.roleSyncMaxAgeSeconds}
/>

<div class="grid grid-cols-1 gap-2 md:grid-cols-2">
	<label class="flex items-center gap-2 text-sm">
		<input type="checkbox" name="clientLoginEnabled" checked={v.clientLoginEnabled ?? true} />
		<span>{t(locale, 'oauth.field.clientLogin')}</span>
	</label>
	<label class="flex items-center gap-2 text-sm">
		<input type="checkbox" name="adminLoginEnabled" checked={v.adminLoginEnabled ?? true} />
		<span>{t(locale, 'oauth.field.adminLogin')}</span>
	</label>
	<label class="flex items-center gap-2 text-sm">
		<input type="checkbox" name="roleClaimRequired" checked={v.roleClaimRequired ?? true} />
		<span>{t(locale, 'oauth.field.roleClaimRequired')}</span>
	</label>
	<label class="flex items-center gap-2 text-sm">
		<input type="checkbox" name="autoCreate" checked={v.autoCreate ?? true} />
		<span>{t(locale, 'oauth.field.autoCreate')}</span>
	</label>
	<label class="flex items-center gap-2 text-sm">
		<input type="checkbox" name="enabled" checked={v.enabled ?? true} />
		<span>{t(locale, 'oauth.field.enabled')}</span>
	</label>
</div>
