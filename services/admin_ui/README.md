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
| A | 工程骨架、主题令牌、i18n 词典、会话装载、概览页 | ✅ 已完成（含与真实 admin-web 的联调） |
| B | 资源页（agents / skills / mcp / llm / workflows） | ✅ 已完成（五页全部迁移） |
| C | 访问控制页（pages / components / roles / mappings / platform-admins / preferences / audit） | ✅ 已完成（七页全部迁移，含浏览器实测） |
| D | workflow 画布作为 island 接入（**不重写画布**） | ✅ 已完成（`/admin/workflows/{code}` 外壳 + `<iframe>` 承载 Python 的画布片段页） |
| E | 下线 Ludic 视图层 | ✅ 已完成（`admin-web` 收敛为纯 JSON API + 登录页 + 画布 island） |

**新旧共存的仲裁点在 `src/hooks.server.ts`**：路径落在 Python 独占清单里就
代理给 Python；没匹配到 SvelteKit 路由的路径也回退给 Python——所以没迁的
页面仍然可用，某个页面删掉对应路由就自动切回旧实现。

设计与取舍见 [V4.10：后台前端迁移到 SvelteKit](../../app/design/v4.10-sveltekit-admin-ui.md)，
剩余阶段在 [TODO](../../docs/design/TODO.md) 里跟踪。

## 本地运行

```bash
npm install
ADMIN_API_BASE_URL=http://127.0.0.1:8001 npm run dev
```

打开 <http://127.0.0.1:5173/admin/>。未登录会被 302 到 Python 的登录页
（`/admin/login`），由 SvelteKit 代理过去。

## 部署

镜像在 `services/admin_ui/Dockerfile`：两段构建——构建段装全量依赖跑
`vite build`，运行段只留生产依赖与产物（非 root、只读 rootfs）。

容器编排在 `deploy/docker-compose.stack.yml` 与
`deploy/docker-compose.admin.yml`。**对外端口归本容器**（`8001` → 容器内
`3000`）：它接管 `/admin/*`，已迁的页面自己渲染，仍属 Python 的路径与
尚未迁完的页面转发回 `admin-web`；`admin-web` 只在 compose 网络内可达。

两个环境变量：

| 变量 | 作用 |
| --- | --- |
| `ADMIN_API_BASE_URL` | 管理端（Python）的**容器内**地址，如 `http://admin-web:8001` |
| `PORT` / `HOST` | 监听地址与端口，默认 `3000` / `0.0.0.0` |

**必须设 `ORIGIN`**（填管理端的公开地址，如 `http://192.168.5.16:8001`）：
`adapter-node` 在没有 `PROTOCOL_HEADER` 时**默认按 https 推导** origin，
而直连部署是 http——推导出来的 origin 与浏览器发来的对不上，SvelteKit
自带的 CSRF 校验会把所有表单 POST 拒掉（`Cross-site POST form submissions
are forbidden`），登录直接失败。放在 TLS 终止的反代后面时改成设
`ORIGIN` 为 https 的公开地址即可。

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
    ├── agents/ skills/ mcp/ llm/ workflows/   # 资源页
    ├── workflows/[code]/        # 画布外壳（iframe 承载 Python 的画布片段页）
    ├── access/pages|components|roles|mappings/  # 访问控制
    ├── platform-admins/ preferences/ audit/     # 其余三页
    ├── locale/+server.ts      # 语言切换
    └── ...
```

## 删除后能撤销

Agent、LLM 端点、MCP 服务器、工作流与平台超级管理员的删除是**硬删除**，但
删除前会留快照：删除响应带回 `undoId`，前端把它放进 `?undo=<id>` 并显示
「撤销删除」条。撤销端点是 `POST /admin/api/undo/{entry_id}`，它按**资源
类型**判定写权限——撤销一个 LLM 端点不该要求 Agent 编辑权。