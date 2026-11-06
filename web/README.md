# Web 页面

`web/` 包含两个零依赖单文件页面，由 FastAPI 同源提供。它们直接调用既有 REST/SSE 接口，不额外暴露服务端能力。

## 功能验证台

入口：`/`，源文件：`index.html`。

验证台用于端到端检查：

- 通过服务端 OIDC 登录，显示当前 username/phone/email，支持登出与 401 回登录。
- 创建会话、恢复历史会话和进行中的 run。
- 查看数据库中已启用的 Agent 和 Workflow。
- 按节点展示流式输出，支持发送、取消、主动断线和重连。
- 展示 `event`、`id`/`seq` 和原始 `data`。
- 实时检查 seq 是否单调、连续且无重复。
- 自动在第 5 个事件后断开，再带 `last_seq` 重连，验证恢复链路。

页面默认每 500ms 查询一次 run 状态，因为流中的 `done` 事件可能先于数据库最终状态回写。

## 后台管理

入口：`/admin`，源文件：`admin.html`。

| 页面 | 能力 |
| --- | --- |
| Agent 配置 | 增删改查模型、提示词、工具白名单和启用状态 |
| Workflow 编排 | 表单维护节点，显式添加边和 `when` 条件 |
| 会话观测 | 使用当前浏览器登录态查看自己的消息历史和进行中的 run |

API Key 只显示 `has_api_key`，不会回填明文。编辑时留空表示保持原值，提交空字符串才会清除密钥。

## 接入注意

浏览器原生 `EventSource` 自动重连会复用原 URL，而 SSE ticket 只有 60 秒有效期。
页面在每次重连前为当前 `run_id` 重新申请 ticket，并从本地保存的 `last_seq`
继续。所有修改请求从非 HttpOnly 的 CSRF Cookie 读取值并放入
`X-CSRF-Token`；OAuth access token 不进入页面存储。

## 相关文档

- [API 与事件协议](../app/api/README.md)
- [主服务入门](../app/README.md)
