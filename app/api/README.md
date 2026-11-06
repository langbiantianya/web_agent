# API 与事件协议

`app/api/` 暴露 REST API、SSE 事件流、用户 OIDC 登录和短期 ticket 鉴权。
路由实现位于 `routes/`，`deps.py` 提供数据库会话、用户服务端会话、CSRF 与续传位置解析。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/agents` | 列出或创建 Agent |
| GET/PUT/DELETE | `/api/agents/{code}` | Agent 详情、更新或删除 |
| GET | `/api/agents/{code}/prompt-preview` | 预览 Skill 注入后的提示词 |
| GET | `/api/tools` | 当前部署可用的工具目录 |
| GET | `/api/tool-audits` | 管理员分页查询工具审计摘要 |
| GET | `/api/tool-audits/{id}` | 管理员查看脱敏参数与限长结果 |
| GET/POST | `/api/admin-users` | 管理员列出或创建数据库管理员 |
| GET/PUT/DELETE | `/api/admin-users/{username}` | 查询、更新或删除管理员 |
| POST | `/api/admin-users/{username}/rotate-key` | 轮换管理员 Key，只返回一次明文 |
| GET/POST | `/api/workflows` | 列出或创建 Workflow |
| GET/PUT/DELETE | `/api/workflows/{code}` | Workflow 详情、更新或删除 |
| GET/POST | `/api/conversations` | 列出或创建会话 |
| GET | `/api/conversations/{id}/messages` | 获取会话消息历史 |
| GET | `/api/conversations/{id}/active-run` | 获取进行中的 run，没有则返回 `null` |
| POST | `/api/conversations/{id}/messages` | 发送消息并立即返回 `runId` |
| GET | `/api/runs/{id}` | run 详情和节点执行记录 |
| GET | `/api/runs/{id}/messages` | run 的最终消息 |
| POST | `/api/runs/{id}/cancel` | 取消生成 |
| GET/POST | `/api/runs/{id}/approval` | 查询或提交人工审批/会签投票 |
| GET | `/api/runs/{id}/stream` | 订阅 SSE 事件流 |
| POST | `/api/auth/sse-ticket` | 获取短期 SSE ticket |
| GET | `/api/auth/providers` | 列出公开登录供应商 |
| POST | `/api/auth/oauth/{provider}/start` | 发起 OIDC + PKCE 登录 |
| GET | `/api/auth/oauth/{provider}/callback` | OIDC 回调 |
| GET | `/api/auth/me` | 当前用户资料 |
| POST | `/api/auth/logout` / `logout-all` | 撤销当前/全部会话 |
| GET/POST/PUT/DELETE | `/api/oauth-providers` | 管理员维护登录供应商 |
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/llm-endpoints` | 列出或新增数据库 LLM 接口配置 |
| GET/PUT/DELETE | `/api/llm-endpoints/{code}` | 查询、更新或删除接口配置 |

Skill 与 MCP 管理接口分别位于 `/api/skills` 和 `/api/mcp/servers`。管理写操作要求
`X-Admin-Key`；环境引导 Key 与数据库管理员 Key 均可使用，具体配置见 `.env.example`。

Skill 生命周期接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/skills/{code}/publish` | 发布草稿 |
| POST | `/api/skills/{code}/restore` | 恢复归档技能为草稿 |
| POST | `/api/skills/{code}/rollback` | 回滚正文与全部资源到指定修订 |

MCP 除工具目录外还提供 resources、resource templates 与 prompts 的实时查询。在线状态明确
标记为实例级，不代表跨实例共识。

LLM 接口配置要求 `X-Admin-Key`，API Key 不回显。多个启用项按 `priority` 数值
从小到大尝试；同一优先级按创建顺序稳定排序。请求尚未输出内容时失败会切换下一项，
已经向客户端输出 token 或 reasoning 后不会重放另一接口。

## 用户登录与请求约定

浏览器登录后只持有两个 Cookie：

- `__Host-wa_session`：HttpOnly、Secure、SameSite=Lax；
- `__Host-wa_csrf`：Secure、SameSite=Strict，前端读取后放入 `X-CSRF-Token`。

本地 HTTP 开发可设 `USER_SESSION_COOKIE_SECURE=false`，Cookie 名会去掉
`__Host-` 前缀。所有 `POST/PUT/PATCH/DELETE` 都校验 CSRF；生产 OIDC 回调还必须
配置固定 `OAUTH_PUBLIC_BASE_URL`。OAuth access token 不进入浏览器存储。

## 创建会话并订阅

下面的 `COOKIE`、`CSRF` 是已登录浏览器会话的两个 Cookie 值：

```bash
BASE=http://127.0.0.1:8000

CONV=$(curl -s -X POST "$BASE/api/conversations" \
  -H "Cookie: wa_session=$COOKIE; wa_csrf=$CSRF" \
  -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"workflow_code":"default_pipeline"}' | jq -r .id)

RUN=$(curl -s -X POST "$BASE/api/conversations/$CONV/messages" \
  -H "Cookie: wa_session=$COOKIE; wa_csrf=$CSRF" \
  -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"content":"帮我规划一次技术分享"}' | jq -r .run_id)

TICKET=$(curl -s -X POST "$BASE/api/auth/sse-ticket" \
  -H "Cookie: wa_session=$COOKIE; wa_csrf=$CSRF" \
  -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' -d "{\"run_id\":$RUN}" | jq -r .ticket)

curl -N -H "Cookie: wa_session=$COOKIE" \
  "$BASE/api/runs/$RUN/stream?ticket=$TICKET&last_seq=0"
```

创建消息的响应是 `202`，生成在后台继续执行。SSE ticket 有效期为 60 秒，
绑定 `user + tenant + run`；每次重连都应重新签发。用户、消息、run、审批和流都按
服务端会话身份查询，客户端不能提交 `user_id`。

单条 SSE 连接最多存活一个 ticket TTL。到期时服务端发送不占 `seq` 的
`ticket_expired` 控制事件，客户端换票重连；新连接会重新校验 session，因此登出和
用户停用最多在一个 TTL 内切断已有流。

## 事件类型

| 事件 | 含义 | 占用 seq |
| --- | --- | --- |
| `node_start` | 节点开始 | 是 |
| `token` | 文本增量 | 是 |
| `reasoning` | 思维链增量 | 是 |
| `tool_call` | 模型请求调用工具 | 是 |
| `tool_result` | 工具执行结果 | 是 |
| `node_end` | 节点结束 | 是 |
| `approval_required` | 流程停在审批节点 | 是 |
| `approval_vote` | 一张会签票被接受 | 是 |
| `approval_timeout` | 审批按截止时间策略收口 | 是 |
| `approved` / `rejected` | 审批结果 | 是 |
| `error` | run 出错 | 是 |
| `done` | run 正常结束 | 是 |
| `: ping` | SSE 注释行心跳 | 否 |

`id:` 字段就是 `seq`。客户端重连时传 `last_seq` 或 `Last-Event-ID`，服务端会从下一条事件继续发送。只有 `done` 是终止事件；审批挂起时连接保持打开。

请求可携带 W3C `traceparent`。服务会校验格式、生成必要子上下文，并在响应头返回当前
`traceparent`；该上下文继续传给工具、studio、dispatcher 和 sandbox。

## 恢复与错误

| 场景 | 行为 |
| --- | --- |
| 页面刷新 | 拉消息历史，再查 `active-run`，从 `last_seq=0` 回放 |
| 网络中断 | 使用 `Last-Event-ID` 或本地 `last_seq` 续传 |
| run 已结束 | 回放剩余事件；事件已归档时合成 `done` |
| 事件已清理 | 返回 `410` 和最终消息地址 |
| 服务重启 | 启动恢复扫描遗留 `running` run，并按租约判断是否可回收 |

常见错误码：`400` 参数错误、`401` 未登录或 ticket 无效、`403` Origin/CSRF
校验失败、`404` 资源不存在或越权、`409` 并发或审批冲突、`410` 事件不可回放、
`422` 敏感内容被前置过滤。

## 相关文档

- [主服务入门](../README.md)
- [核心编排与数据流](../core/README.md)
- [V1 API 设计](../design/v1-foundation.md#8-api-设计)
