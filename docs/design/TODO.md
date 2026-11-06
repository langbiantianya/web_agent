# 设计 TODO

本页只保留当前仍未交付的工作。已完成能力、替换关系和验证记录在
[设计总目录](README.md) 与 [V4.4](../../app/design/v4.4-governance-runtime.md) 中维护。

优先级：`P0` 阻断生产采用，`P1` 重要缺口，`P2` 体验与扩展。

截至 V4.6，OAuth/OIDC 主链路已经落地。尚未完成的生产 P1 项集中在真实 IdP
联调、登录限频/过期 attempt 清理，以及存量会话归属处理。

## 隔离执行与成本

- [ ] **P2：预启动 sandbox 容器池**
  当前启动期已检查并拉取镜像，消除首个请求的镜像拉取成本；尚未预先创建容器。
  若继续优化冷启动，需要为每个预热容器预分配 payload 挂载、设置回收租约，并验证
  长时间空闲容器的安全与内存成本。

- [ ] **P2：多 dispatcher 之间共享配额和预算**
  当前租户/技能租约在一个共享 dispatcher 内跨多个 studio 实例生效。多个独立
  dispatcher 之间尚未共享状态；拆分 dispatcher 前必须把准入状态迁到共享存储，
  否则不能宣称全局一致。

- [ ] **P2：累计计费账本**
  当前按真实 CPU 秒、内存峰值和启动耗时计算“活动请求成本”，用于准入和运行观测；
  尚未把每次消耗持久化为可对账的累计账单，也未接入云厂商价格目录。

## 用户身份与会话

- [ ] **P1：OAuth 生产加固与真实 IdP 联调**
  [V4.6 方案](../../app/design/v4.6-user-oauth-conversation.md) 中的 OIDC、PKCE、
  HttpOnly 服务端会话、CSRF、供应商管理和 conversation owner 隔离已经实现，
  并已通过专用 PostgreSQL 测试库验证。
  上线前仍需用实际 `AUTH_DEFAULT_TENANT` 对应的 IdP 完成浏览器联调，并增加
  start/callback 限频、过期 `oauth_login_attempt` 周期清理和 provider 故障监控。

- [ ] **P1：存量 conversation 归属迁移**
  当前 `owner_user_id` 仍允许为空，以承接无法证明真实身份的存量数据。
  需要提供显式管理员映射工具，将已确认会话写入目标 user ID；完成无主数据归档后，
  再把 owner 改为非空并删除旧 `conversation.user_id` 列。不能按 username 自动认领。

## MCP

- [ ] **P2：MCP authorization-code 用户委托**
  当前已完成 OAuth `client_credentials` 机器身份、refresh token、加密缓存和撤销。
  需要用户同意页与浏览器回调的 MCP 服务仍不可用，后续应接独立 OAuth 前端。

- [ ] **P2：MCP 跨实例在线状态**
  当前在线状态明确是 `online_scope=instance`，不是集群共识。若控制台要求跨实例统一展示，
  需要引入共享健康状态与过期租约。

## 审计与归档

- [ ] **P2：超大工具结果对象存储归档**
  工具审计结果默认最多保存 64K 字符。需要无限归档时，应接对象存储，并补齐签名下载、
  生命周期、访问控制和租户隔离；数据库账本继续只保存可查询摘要。
