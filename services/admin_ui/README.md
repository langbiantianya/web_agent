# admin_ui —— SvelteKit 管理端（V4.10）

后台前端的下一代实现：**SvelteKit 3 + Svelte 5（runes）+ TypeScript**。

## 它负责什么

只负责**渲染与交互**：页面布局、表单、列表、主题与语言。

## 它不负责什么

**鉴权与授权仍然只有 Python 那一侧**（`services/admin_web`）：

- 会话 Cookie 的签发与校验；
- CSRF 三方校验（Cookie + 表单 + Session 摘要）；
- Casbin 页面/组件级权限判定；
- 审计写入、通知发送、超级管理员口令轮换。

浏览器**永远不直接访问 Python 管理端**：所有请求都经 `src/lib/server/admin-api.ts`
在服务端转发，Cookie 不出服务端，CSRF 令牌由服务端持有。

## 迁移状态

后台前端正从 **Ludic SSR + 手写 DOM JS** 迁到这里，分 A–E 五个阶段推进，
每个阶段都能单独回退（关掉本容器，反向代理直指 `admin-web` 即可）。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| A | 工程骨架、主题令牌、i18n 词典、会话装载、概览页 | 已就位 |
| B | 资源页（agents / skills / mcp / llm / workflows） | 未开始 |
| C | 访问控制页（pages / components / roles / mappings / platform-admins / preferences / audit） | 未开始 |
| D | workflow 画布作为 island 接入（**不重写画布**） | 未开始 |
| E | 下线 Ludic 视图层 | 未开始 |

设计与取舍见 [V4.10：后台前端迁移到 SvelteKit](../../app/design/v4.10-sveltekit-admin-ui.md)，
剩余阶段在 [TODO](../../docs/design/TODO.md) 里跟踪。

## 本地运行

```bash
npm install
ADMIN_API_BASE_URL=http://127.0.0.1:8001 npm run dev
```

打开 <http://127.0.0.1:5173/admin/>。未登录会被 302 到 Python 的登录页
（`/admin/login`），由 SvelteKit 代理过去。

## 校验与测试

```bash
npm run check   # svelte-check + tsc
npm run test    # vitest
npm run build   # 生产构建
```

## 目录

```
src/
├── app.css                    # Tailwind 4 + 主题令牌
├── hooks.server.ts            # 代理 Python 独占路径 + 装载会话
├── lib/
│   ├── i18n.ts                # 双语词典（构建期固定，不做运行时 DOM 翻译）
│   ├── types.ts               # 与 /admin/api/* 对齐的类型
│   ├── server/admin-api.ts    # 服务端转发客户端
│   └── components/            # 壳层与通用组件
└── routes/
    ├── +layout.server.ts      # 会话 + 语言
    ├── +layout.svelte         # 壳层（侧栏 + 顶栏）
    ├── +page.svelte           # 概览
    ├── locale/+server.ts      # 语言切换
    └── ...
```