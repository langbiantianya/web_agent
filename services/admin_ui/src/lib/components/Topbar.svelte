<script lang="ts">
	import { pythonPath } from '#lib/paths';
	import { t, type Locale } from '#lib/i18n';
	import type { AdminPrincipal } from '#lib/types';

	interface Props {
		principal?: AdminPrincipal;
		locale: Locale;
	}

	let { principal, locale }: Props = $props();
</script>

<header class="flex items-center justify-end gap-3 border-b border-line bg-surface px-5 py-3">
	<span class="text-sm text-ink-muted">
		{principal?.isPlatformSuperAdmin ? '平台所有者' : principal?.username}
	</span>
	<span class="text-sm font-medium">{principal?.username}</span>
	<form method="POST" action={pythonPath('/logout')}>
		<button class="rounded-card border border-line px-3 py-1.5 text-sm" type="submit">
			{t(locale, 'shell.signOut')}
		</button>
	</form>
</header>