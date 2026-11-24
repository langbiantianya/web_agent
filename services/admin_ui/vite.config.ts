import adapter from '@sveltejs/adapter-node';
import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// 强制 runes 模式（排除 node_modules 里的库）。Svelte 6 之后可移除。
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			// 自建 Node 容器运行（与 admin-web 并列的独立服务），不用 adapter-auto。
			adapter: adapter({ out: 'build' }),
			// 后台挂在 /admin 下，与现有 admin-web 的公开路径保持一致。
			paths: { base: '/admin' },
		})
	]
});