# V2：多实例与 LangGraph

> V2 解决多实例事件分发、回放与 run 归属，并用 LangGraph 替换 V1 自研控制流。

> 章节编号沿用拆分前文档，便于旧链接与讨论继续使用。

> 导航：[上一版：V1](v1-foundation.md) · [主服务索引](README.md) · [下一版：V3](v3-orchestration.md)

---

## 13. V2：Redis 多实例

V1 的目标形态是单实例。V2 要做的是让它能在**多个进程之间**正确工作——不是简单地把
uvicorn 起两台，而是把「哪些状态不能留在进程内存里」这个问题系统性地解决掉。

### 13.1 先定位：多实例下哪些状态会出问题

逐条审查 V1 的六处进程内状态，结论如下。前两项是**硬伤**：

| 状态 | V1 位置 | 多实例下的后果 | V2 方案 |
| --- | --- | --- | --- |
| 事件广播 | `InMemoryBackend` | B 实例收不到 A 实例吐的字 | Redis Pub/Sub |
| **落库缓冲** | `EventWriter._batch` | **B 实例的 `hub.flush()` 是空操作 → 尾部 token 丢失**，§12.3 ① 修的 bug 原样复活 | 回放源换成 Redis Stream，`flush()` 不再是正确性前提 |
| **启动恢复** | 扫全表把 running 标 error | **B 实例一启动就把 A 实例正在跑的 run 全杀掉** | 归属租约，只回收无主的 |
| 取消标志 | `_RunState.cancel_requested` | B 实例点取消，A 实例收不到 | 控制频道 |
| 活跃判断 | `_states` 里有键 | 跨实例判断不了 | 租约 TTL |
| seq 计数器 | 本地计数 | 单 run 只由一个实例生成，不冲突 | 保留本地，Stream ID 冲突作护栏 |

其中「启动恢复误杀」最严重：多活反而比单活更糟，用户眼看着自己的回答在生成中途被判死刑。

### 13.2 回放源：为什么必须是 Redis Stream

V1 里「回放读 MySQL」直接导致 `hub.flush()` 成为续传正确性的前提——因为
`EventWriter` 批量异步落库。单进程时 flush 就在同一进程里，没问题；**多实例做不到**：
SSE 在 B 实例、EventWriter 在 A 实例，B 调 `flush()` 是空操作。

所以 V2 把回放源换成 Redis Stream。`emit()` 时事件就**同步** `XADD` 进 Stream，
任意实例的 SSE 都能 `XRANGE` 到它——`flush()` 从此只是「让 MySQL 归档更及时」的优化。

Stream 条目 ID 固定写成 `<seq>-0`。Redis 要求新 ID 严格大于末位，而 seq 在单个 run 内
单调递增，于是断点续传直接就是 `XRANGE key (42-0 +`，不必按字段过滤。万一两处同时给
同一个 run 发号，Redis 会拒绝较小的 ID——这是单写者约定的运行时护栏，而不是悄悄写进
一条乱序数据。

数据流：

```text
emit(seq=42)
  ├─ XADD    P:run:<id>:ev   42-0 …     ← 持久化 + 回放源（同步，跨实例可见）
  ├─ PUBLISH P:run:<id>      {...}      ← 实时推送（跨实例）
  └─ 本地 EventWriter → MySQL           ← 长期归档（批量，保留 7 天）
```

**顺序不能换**：先落再播，保证订阅者收到的任何事件都必定已经可被回放。

MySQL 仍保留为长期归档，因此 `RedisEventLog.replay` 在 Stream 为空时会**回落到 MySQL**——
这不是可选兜底而是必需的：Stream 有 TTL，而启用 Redis 之前产生的 run 压根没有 Stream，
只读 Stream 会让这些 run 一被续传就读不到内容。

### 13.3 续传正确性的时序论证（以及它被打破过一次）

设 `t1` = Redis 确认 SUBSCRIBE 的时刻，`t2` = 回放执行 XRANGE 的时刻。任意事件 E 满足
`publish_time > append_time`，于是

> E 不在回放里（`append_time > t2`）与 E 不在实时队列里（`publish_time < t1`）
> **不可能同时成立**——前提是 `t1 < t2`。

所以「回放 ∪ 实时队列」是完整的，`hub.flush()` 不再需要。

**但这个前提曾经被打破，而且真的漏了字。** 初版实现里 `subscribe()` 只创建 pump 任务就
返回，SUBSCRIBE 还在网络上飞。此时 `t2 < t1` 完全可能（XRANGE 与 SUBSCRIBE 是两条连接上
的并发往返，没有先后保证），落在 `(t2, t1)` 窗口里的事件两头落空。

双实例实测抓到了：一次「读 30 条就断开、重连接 415 条」的续传里，`seq=33` 没送达，
拼接文本少 2 个字。查 Redis Stream 与 MySQL 都是完整的 445 条——**服务端没丢，是订阅
窗口**。修法是恢复 V1 的不变量：`BusBackend.subscribe` 改为协程，**必须 await 到 Redis
确认 SUBSCRIBE 才返回**，`sse_writer` 第 ① 步随之 await。

> 这也是为什么 `subscribe` 不能图省事写成同步的：它必须是「返回即已生效」的语义，
> InMemory 实现内部没有任何 await 所以零开销，Redis 实现则老实等一次网络往返。

### 13.4 归属租约与取消信号

「谁在管这个 run」变成 Redis 里一把带 TTL 的租约：

| 键 | 值 | TTL | 用途 |
| --- | --- | --- | --- |
| `P:run:<id>:owner` | 实例标识（`主机名:PID:随机后缀`） | `REDIS_ACTIVITY_TTL`，按 TTL/3 续期 | 判断 run 是否还有主 |
| `P:run:<id>:cancel` | `1` | 较大 | 兜住「取消早于订阅到达」的窗口 |
| `P:run:<id>:ctl` | Pub/Sub | — | 取消等控制指令 |

- **启动恢复**只回收**没有主**的 run。实例被 `kill -9` 时没人续期，键自然过期，
  不需要任何额外的清理动作、心跳表或实例注册表。
- **取消**走控制频道，同时写取消键、订阅方上线后主动补查。只做前者会漏取消，
  只做后者会有延迟（用户点了停止却要等几秒才停）。
- **`is_cancelled` 仍然只读进程内的布尔字段**——它每个 chunk 都要轮询一次，那里绝不能
  出现 await 或 Redis 往返。跨实例的部分由控制频道在**收到信号的那一刻**解决掉。
- **`is_active` 跨实例语义**单独提供 `is_active_anywhere()`；`is_active()` 保持只看本实例。

### 13.5 配置与降级策略

```dotenv
REDIS_ENABLED=false          # 默认关：单实例行为，测试全部依赖它
REDIS_URL=redis://192.168.1.5:6379/0
REDIS_KEY_PREFIX=webagent    # 多套环境共用一个 Redis 时做隔离
REDIS_STREAM_MAXLEN=20000    # 每个 run 的 Stream 条数上限（近似裁剪）
REDIS_ACTIVITY_TTL=120       # 租约 TTL（秒）
REDIS_POLL_INTERVAL=1.0      # 订阅轮询间隔，决定退订收尾的快慢
```

**开启后没有降级路径**：Redis 连不上时服务直接启动失败。这是刻意的——静默退回进程内总线
会造成「服务能跑、跨实例收不到事件」的分裂大脑，比启动失败难查得多。单实例开发请显式关掉。

### 13.6 抽象方式

延续 V1 已有的「Protocol + 双实现 + 单例」套路，让业务代码里没有一处 `if redis_enabled`：

| 抽象 | V1 实现 | V2 实现 |
| --- | --- | --- |
| `BusBackend` | `InMemoryBackend` | `RedisPubSubBackend`（Pub/Sub 实时） |
| `EventLog` | `DbEventLog`（读 `run_event`） | `RedisEventLog`（读 Stream，空则回落 MySQL） |
| `RunLease` | `NoopLease` | `RedisLease`（租约 + 取消频道） |

`get_bus()` / `get_event_log()` / `get_lease()` 按配置选实现，三处必须一致——否则会出现
「事件写进 Redis、回放却去 MySQL 找」这种错配，症状是续传大面积失败且极难定位。

### 13.7 单写者约定

一个 run 只由接下 `POST /messages` 的那个实例负责编排，别的实例只能读。这条约定撑起了两处
设计：seq 分配用本地计数器（省掉每个 token 一次 Redis 往返），以及 Stream 条目 ID 直接取
seq。V2 **没有实现 failover**：实例挂掉后它持有的 run 由租约过期 → 启动恢复标 error，
不会被另一个实例接管重跑。

### 13.8 实现过程中发现并修复的缺陷

除 §13.3 那个漏字 bug 外，V2 落地时还踩到三处，都已修复：

| # | 缺陷 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | **Redis 订阅 / 回放之间漏事件** | `subscribe()` 不等 SUBSCRIBE 确认，见 §13.3 | `subscribe` 改协程，必须 await |
| 2 | **cancel 掉阻塞在读上的订阅任务会让后续关闭永久挂起** | pump 阻塞在 `pubsub.get_message()`，强行 cancel 打断这次读，redis-py 的连接锁留在不一致状态，之后的 `unsubscribe()/aclose()` 抢同一把锁就死等。现象是**挂在几十行之外的用例上** | 退订只置关闭标志、让 pump 在下一个轮询周期自行退出；`REDIS_POLL_INTERVAL` 决定收尾快慢 |
| 3 | **`PUT /api/workflows/{code}` 带 definition 必 500** | `payload.model_dump()` 已把嵌套模型转成 dict，代码又对该 dict 调 `.model_dump()` | 从 `payload.definition`（模型对象）重新 dump |
| 4 | **测试套件被开发者本地 `.env` 污染** | `env_file=".env"` 是相对路径，pytest 从项目根跑会命中本地 `.env`；开了 Redis 后整个套件切到 Redis 模式，不同用例的 run id 从 1 开始互相撞车 | `conftest.py` 强制 `REDIS_ENABLED=false` |

其中 #3 是 V1 就存在的 bug，此前测试只覆盖了 POST 建 Workflow，一直没人发现。

### 13.9 验证结果

- **19 个 Redis 集成测试**（`tests/test_redis.py`）：键布局、Stream 断点续传、重复 seq
  护栏、TTL、MySQL 回落、跨实例 Pub/Sub、跨实例取消、取消早于订阅到达的补查、启动恢复
  归属判定，以及两条跨实例续传主线。
- **V1 回归 74 个用例全绿**，确认默认关闭 Redis 时行为逐字不变。
- **双实例端到端**：两个独立 uvicorn 进程（实例标识不同），在 B 实例发消息、A 实例收流，
  中途断开后带 `last_seq` 在 A 实例重连——445 个事件 seq 严格连续、无重复、无乱序，
  拼接文本与 MySQL 落库、与假模型输出三方逐字一致（873 字）。

外部依赖抖动会伪装成代码故障，因此做了两件事：

- `connect_redis` 带 3 次退避重试。实测遇到过一轮测试跑到一半 Redis 拒连
  （`Error 22 ... 远程计算机拒绝网络连接`），两秒后它自己恢复；没有重试的话，
  那一次会变成 19 个用例同时 ERROR。
- 测试夹具在连不上时**跳过**而不是报错。真正的回归应当表现为「Redis 通着但用例不过」。

---

## 14. V2-b：用 LangGraph 替换自研编排器

V1 的 `execute_run` 是一段 `for node in workflow.linear_order()`。它能跑，但「控制流」
这件事被写死在了 Python 循环里——想加分支、判断路由、并行汇聚，都得回来改这个循环。
V2-b 把控制流交给图，节点执行逻辑一行不动。

### 14.1 拆分点：NodeOps / NodeRunner

```python
graph = build_run_graph(definition, runner)
final = await graph.ainvoke(initial_state, {"recursion_limit": len(nodes) + 10})
```

`RunState` 只放**跨节点流动**的数据（context / upstream / usage）。每 run 固定不变的量
（run_id、agents、hub……）不进 state，它们是 `NodeRunner` 的构造参数——放进 state 只会让
LangGraph 每个 super-step 都去序列化一遍本不需要持久化的东西。

`NodeOps` 是 Protocol，`Orchestrator` 实现它（`set_current_node` / `run_with_retry` /
`persist_message` 三个方法），因此 `run_graph.py` 不依赖 `orchestrator.py`，反之也不依赖。
节点闭包只做一件事：把 state 转交给 `execute_node`。

### 14.2 连线来自 definition.edges，不是 nodes 声明顺序

这是本次迁移最关键的一处。`build_run_graph` 按 `edges` 连线，而不是假设「按 nodes 顺序
串起来」。于是 V3 加条件边 / DAG 时，改的只有这一个函数，`execute_node` 一行都不用动。
`tests/test_run_graph.py` 里 `test_execution_order_follows_edges_not_declaration_order`
专门钉住这一点：nodes 声明为 `[b, a]`、edges 为 `a→b`，必须按 `a→b` 执行。

节点名用 `node.id`，它同时是事件里的 `node` 字段、`upstream` 的键和 `run_step` 的外键——
用它当图节点名能让日志里的图和业务数据对得上。

### 14.3 刻意不挂 checkpointer

`langgraph-checkpoint-redis` 提供了 `AsyncRedisSaver`（支持 `.with_ttl()`），技术上完全
可以接。**不接**，理由有三条，每条都能用本项目的实测数据说明：

**粒度差两个数量级。** `EventType` 七种里 `TOKEN` / `REASONING` 是逐块推进的。§13.9 的
双实例实测里，一次三节点流水线产生 **445 条事件**；而 checkpointer 的粒度是 super-step，
一个节点执行完存一份，即 4 个 checkpoint。**445 : 4 ≈ 每个 checkpoint 里压着 110 条事件。**
换成它，客户端在第 137 个 token 断线就只能从节点边界重放，前端打字机退化成「整段冒出来」
——这正是 V1 花力气修好的「尾部 token 丢失」要换个方向再犯一次。

**存的东西类型不同。** `run_event` 存的是 `RunEventDTO(run_id, seq, event_type, payload)`
——追加、不可变、带全局序号的事件流。LangGraph 存的是 `Checkpoint(channel_values,
channel_versions, pending_writes, next)`——**状态快照**，每个 super-step 覆盖上一份。
快照不能当事件流读：打字机、重放、以及「流文本 == 库文本」这类断言，全都建立在能按 seq
逐条重放上。

**checkpointer 不提供互斥。** 读 `BaseCheckpointSaver` 的 docstring，通篇只讲状态持久化
与 `thread_id` 主键，没有任何并发控制语义。而 §13.4 的归属租约解决的正是「别让两个实例
同时编排同一个 run」。换过去的结果是事件流换掉了，租约、取消控制频道、启动时回收无主 run
三样一个不少，架构复杂度反而多一份。

「用 `get_state_history()` 从快照差分推事件」这条路也堵死了：要让 LLM 节点变成逐 token
checkpoint 的子图，每个 token 一次写——比现在每 token 一次 `XADD` **更贵**；token 增量
必须进 state channel 才能被差分到，于是每次 token 都要重写累积全文，检查点体积 O(n²)；
差分推出来的 `seq` 也没有「无缺口」性质，因此没有 `XADD <seq>-0` 那道天然护栏。

**结论**：`run_event` + Redis Stream 是唯一权威事件日志。它服务的是「对外事件流」这个
消费者，checkpointer 服务的是「恢复执行」这个消费者，两者不冲突也不可互相替代。

> **V3.1 兑现说明**：本节的三条论证**一条都没变**——checkpointer 依然不适合当事件日志，
> 事件流依然只由 `run_event` + Redis Stream 负责。V3.1 挂了 checkpointer，但它服务的是
> 另一个消费者：「图停在哪、恢复时从那儿继续」。本节的结论限定在「事件流」这个用途上，
> 见 [§16.3](v3-orchestration.md#163-暂停机制原生-checkpointer--interrupt)。

### 14.4 保留在节点内的重试与取消检查

LangGraph 有 `RetryPolicy`，但它和我们要的不是一回事：

- **重试**：我们要「指数退避 + 写 `run_step` 记录每次尝试 + 取消时立即中止」。`RetryPolicy`
  只管重试次数，退避策略、每次尝试的可观测性都要绕出去自己实现。留在节点函数里更可控。
- **取消检查**：必须放在**每个图节点的开头**。放到图外就没有「进入下一节点前」的统一
  拦截点——图不会给你一个节点间的钩子。这里保留 `hub.is_cancelled`，它只读本地布尔字段，
  每 chunk 轮询一次，那里不能出现 await（见 §13.4）。

### 14.5 V1 的 DAG 拒绝校验仍然保留

`len(edges) != len(nodes) - 1` 即拒绝，尽管 LangGraph 让 DAG 变得容易。路线图把 DAG 放在
V3，本次不擅自扩大范围。识别为 V3 的低成本改动点：校验规则放宽 + `build_run_graph` 支持
条件边，两处互不干扰。

> **V3 兑现说明**：V2-b 写下的这两个低成本改动点已在 V3 全部落地，见
> [§15](v3-orchestration.md#15-v3dag--条件边)。
> 本节保留原样，作为「当时为什么不擅自扩大范围」的记录。

### 14.6 依赖副作用

`pip install langgraph` 把 `websockets` 从 17.2 降到 16.1.1（langchain-core 1.6.12、
langgraph-checkpoint 4.3.0 一并引入）。已实测 `uvicorn.protocols.websockets.websockets_impl`
仍可正常导入，但会打 `UvicornDeprecationWarning`——uvicorn 已计划把默认实现换成
`websockets-sansio`。不影响 SSE（SSE 走的是 `sse-starlette`），仅此记录。

### 14.7 验证结果

- **9 个图构建测试**（`tests/test_run_graph.py`）：含执行顺序按 edges、空图、单节点、
  节点名对齐、异常原样冒泡。
- **全量 102 个用例全绿**（43.6s），默认关闭 Redis 时行为与 V1 逐字不变。
- 堆栈确认 `graph.ainvoke` 真的在调用链上，不是「import 了但没走到」——
  节点报错时堆栈是 `langgraph/_internal/_runnable.py:522 ainvoke → run_graph.py:195
  _run → execute_node`。
- **迁移后重跑双实例端到端**（B 实例编排、A 实例收流、中途断开后带 `last_seq` 在 A 重连）：
  277 个事件 seq 严格连续、无重复、无乱序，拼接文本 540 字与 MySQL 落库、与假模型三段
  输出三方逐字一致，run 终态 `done`、`last_seq=277` 与流末 seq 相等。

**已知代价**（诚实记录，非缺陷）：每个 token 一次 `XADD`，一个万字 run 就是万条 Stream
条目，直到 `MAXLEN ~` 裁剪。这不是架构问题而是调参问题（`redis_stream_maxlen`）——换
checkpointer 只会更贵，不会更省。

---
