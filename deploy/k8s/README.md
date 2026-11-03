# studio 的部署产物

这三个文件对应 skill-service §7（沙箱加固）、§13（部署与扩缩）：

| 文件 | 内容 |
| --- | --- |
| `studio-deployment.yaml` | Deployment（replicas ≥ 2）+ PodDisruptionBudget |
| `studio-service.yaml` | **headless** Service |
| `studio-networkpolicy.yaml` | NetworkPolicy（入向只放 web_agent，出向全禁） |

镜像在 `services/studio/Dockerfile`。

---

## 真实环境实测记录（Docker）

> 记录日期：2026-11-03。机器：Alpine 3.24 / Docker 29.9.2 / 4 vCPU / 1884 MB。
> 编排：**Docker Compose**（`deploy/docker-compose.lab.yml`），不是 K8s。

这份清单**没有在真实 K8s 集群上跑过**（开发环境没有集群）。但镜像本身与容器配置
已经在**真 Docker** 上逐条验过了，下面是实测到的与实测没到的。

### 已实测（真 Docker）

| 项 | 怎么验的 | 结果 |
| --- | --- | --- |
| 镜像能构建 | `docker compose build studio`（多阶段、node 二进制拷贝、apt 装 bash/tini） | ✅ 369 MB |
| 探针在镜像里能跑 | `docker run --rm webagent/studio:1.0.0 python -m services.studio.healthcheck` | ✅ 连得上、`ready=true` 时退出 0 |
| `readOnlyRootFilesystem: true` 下 `/tmp` 够用 | compose 里 `read_only: true` + `tmpfs: /tmp:size=64m`，脚本照常写临时文件 | ✅ |
| **禁出网** | studio 只挂 `internal: true` 的网络（无默认网关） | ✅ 容器内解析外网域名直接失败（`Temporary failure in name resolution`），但能连到 `web-agent:8000` |
| **零凭据** | 遍历容器环境变量与文件 | ✅ 只有 `STUDIO_*` 运行参数；脚本看到的环境变量只有 `PATH/LANG/TZ/HOME/PYTHON*` 六项 |
| 非 root | `docker exec studio id` | ✅ `uid=1000(studio)` |
| drop capabilities | `/proc/self/status` | ✅ `CapEff/CapPrm/CapBnd` 全为 0 |
| `no-new-privileges` | `/proc/self/status` | ✅ `NoNewPrivs: 1` |
| 进程组隔离 | 脚本起子进程后超时 | ✅ 进程组被杀（子进程状态变 `Z`） |
| 队列闸门 | 并发 2 + 队列 4，发 12 个请求 | ✅ 接受 6 个、拒 6 个（`RESOURCE_EXHAUSTED`：等待队列已满（4）） |
| studio 宕机 | `docker stop studio` | ✅ web_agent 侧拿到「连不上 studio（studio:9000）」，**不抛异常打断 run** |
| gRPC 契约 | 直接打 `Exec`/`Health` | ✅ `exit 1` 是 `COMPLETED` + `exit_code=1`（gRPC 成功 ≠ 脚本成功）；超时是 `TIMEOUT` |
| 容器级 OOM | 256 MB 限额 + 脚本申请 1 GB | ✅ 内核 OOM killer **杀脚本、studio 存活** → `LIMITED` + `exit_code=-1` + 「内存超出容器限额」；cgroup 的 `oom_kill` 计数 +1 |

### 实测挖出来的两个 bug（已修）

这两个都是「本地测试全绿、真容器才炸」的，写在这里是因为它们**只在真部署里**才会
被触发：

1. **在跑 gRPC 的进程里 fork 会崩**（`services/studio/__init__.py`）。
   studio 要 fork 子进程跑脚本，而 gRPC 的 C 核心为支持多进程注册了
   `pthread_atfork` 处理器；「fork 时别的线程正在 gRPC 里」时它会放弃清理，
   子进程带着坏掉的 epoll 状态去 exec，exec 之前就被 glog 的断言 `abort()` 掉——
   脚本根本没跑起来，调用方收到 `exit_code=-6`（SIGABRT）、stdout/stderr 全空。
   修法：`GRPC_ENABLE_FORK_SUPPORT=0`，且必须在 `import grpc` **之前**设好。
   → `tests/test_studio_runtime_guards.py`
2. **容器里没有 init 进程会攒僵尸**。脚本被超时杀掉时，它自己起的子进程会被重新
   挂到 PID 1（也就是 studio）底下；python 不是 init、不回收孤儿，被杀死的孙进程
   变成僵尸（`/proc/<pid>` 还在、状态 `Z`），PID 表被慢慢占满。
   修法：镜像里用 `tini` 当 PID 1（`ENTRYPOINT ["/usr/bin/tini", "--"]`）。

### 仍未实测（只差 K8s 本身）

- NetworkPolicy 是否真的拦住出向——取决于集群的 CNI，Docker 上是用
  `internal: true` 的网络等效实现的，**不等于** K8s 的 NetworkPolicy 生效；
- headless + `round_robin` 是否真把流量摊到多个 Pod 上；
- `terminationGracePeriodSeconds` 与 drain 超时的配合；
- 镜像推到 registry 后的拉取与 digest 钉版。

### 容器级 OOM：三个实测出来的结论

1. **判定靠退出码**：脚本被 SIGKILL 杀掉时，退出码是 `-9`（POSIX 的 `subprocess` 记法）
   或 `137`（`128+9`，容器与 shell 的记法）。两种都要认，都归 `LIMITED`
   （skill-service §13.2）。
2. **「为什么被杀」靠 cgroup 佐证**：`/sys/fs/cgroup/memory.events` 里的 `oom_kill`
   计数在执行期间涨了，才说明确实是 OOM killer 干的。K8s 里同样读得到（cgroup v2）。
3. **`oom_score_adj` 要走 `/proc` 路径**：`prctl(PR_SET_OOM_SCORE_ADJ, 1000)` 在容器里
   **返回 0 却什么都不做**（实测，带不带 `argtypes`、root 还是非 root 都一样）；
   **写 `/proc/self/oom_score_adj` 才有效**。所以 `STUDIO_OOM_SCORE_ADJ` 这个配置项
   实现的是「写 proc 文件」，并且**回读验证**——不信 `prctl` 的返回值。

   实测内核只允许把值往「更易被杀」的方向调（往下调要 `CAP_SYS_RESOURCE`），
   而 K8s 的 `securityContext` 恰好把 `CAP_SYS_RESOURCE` 掉了——但这条不冲突：
   我们要的正是「更易被杀」。

### 部署前请在目标集群里先跑一遍下面这三条

```bash
# 1) 镜像构建
docker build -f services/studio/Dockerfile -t webagent/studio:1.0.0 .

# 2) 探针在镜像里能跑通
docker run --rm webagent/studio:1.0.0 \
  python -m services.studio.healthcheck --addr 127.0.0.1:9000

# 3) NetworkPolicy 真的生效（进了 studio Pod 再往外连）
kubectl exec -it deploy/studio -- curl -m 3 https://example.com
```

> 第 1 条建议加上 `--build-arg APT_MIRROR=mirrors.aliyun.com`
> `--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`——
> 官方源从国内拉经常中途断流（apt 能卡十几分钟）。

---

## 部署步骤

```bash
# 1) 建命名空间（按你自己的规范）
kubectl create namespace web-agent
kubectl label namespace web-agent name=web-agent

# 2) 打镜像并推到仓库
docker build -f services/studio/Dockerfile -t registry.example.com/webagent/studio:1.0.0 .
docker push registry.example.com/webagent/studio:1.0.0

# 3) 改掉清单里的镜像地址、命名空间标签、节点选择器，然后
kubectl apply -f deploy/k8s/studio-deployment.yaml
kubectl apply -f deploy/k8s/studio-service.yaml
kubectl apply -f deploy/k8s/studio-networkpolicy.yaml
```

## web_agent 侧怎么接上

```bash
# STUDIO_EXEC_ADDR 用 **headless Service 的名字**。
# 普通 ClusterIP Service 会让 gRPC 长连接固定在一个 Pod 上，扩缩容白扩。
STUDIO_EXEC_ADDR=studio:9000
STUDIO_EXEC_REQUIRED=true
STUDIO_EXEC_LB_POLICY=round_robin   # 默认值，显式写出来是为了让人看见
```

`STUDIO_EXEC_LB_POLICY` 是 Phase 3 新增的第 6 个配置项：客户端建通道时给地址
加 `dns:///`` 前缀并指定 `round_robin`，这样 DNS 返回的多个后端才会被轮询到。
用 gRPC 默认的 `pick_first` 的话，它只连第一个解析结果，之后再也不换。

---

## 几条容易踩的地方

1. **headless 不是可选项。** 普通 ClusterIP Service + gRPC 长连接 = 永远只有一个
   Pod 在干活，而且**看不出任何异常**（服务完全健康，只是没吃满）。
2. **`terminationGracePeriodSeconds` 必须大于 drain 超时。** 默认 40 秒 vs
   drain 30 秒。反过来的话 K8s 会在 drain 结束前 SIGKILL，在跑的执行被腰斩。
3. **零凭据是重点，不是顺带。** studio 容器里只要有一个能用的 key，一次提示词
   注入就够了：模型让脚本读出那个 key 并打印，输出会回灌进 prompt，再由模型写
   进最终答案。清单里的 `env` 段故意只有 `STUDIO_*` 运行参数。
4. **`/tmp` 是 tmpfs，占的是容器内存。** 所以 `emptyDir.sizeLimit` 要单独限，
   别让它跟 `resources.limits.memory` 叠加超限。
5. **探针用 `exec` 而不是 `grpc:`。** K8s 的 gRPC 探针发的是标准健康检查协议
   `grpc.health.v1.Health/Check`，跟契约里的 `Health` 对不上。
6. **镜像应该钉到 digest。** 清单里现在只钉了 tag；tag 被重新推送就等于换了
   镜像（skill-service §15 的供应链风险）。
7. **PID 1 必须是 init 进程。** 镜像里已经用 `tini` 当 PID 1（`ENTRYPOINT
   ["/usr/bin/tini", "--"]`）。没有它的话，脚本被超时杀掉后它起的子进程会变成
   **僵尸**留在容器里（python 不是 init、不回收孤儿），PID 表被慢慢占满。
8. **studio 进程里不要在 gRPC 之上 fork。** studio 要 fork 子进程跑脚本，所以
   `GRPC_ENABLE_FORK_SUPPORT=0` 是在**代码里**设的（`services/studio/__init__.py`，
   必须在 `import grpc` 之前）。这一条不需要在清单里配——它是代码行为。
   真容器上实测过：不关的话，脚本会以 `exit_code=-6`（SIGABRT）收场，
   stdout/stderr 全空，而且**间歇性**发生（取决于 fork 那一刻有没有别的线程
   在 gRPC 里），本地测试抓不到。
9. **OOM 判定要认两种退出码。** 被 OOM killer 杀掉的脚本，退出码是 `-9`（POSIX 的
   `subprocess` 记法）或 `137`（`128+9`，容器与 shell 的记法）——只认一个就会漏判。
   另外「被 SIGKILL 了」不等于「超内存了」：cgroup v2 的
   `/sys/fs/cgroup/memory.events` 里的 `oom_kill` 计数才是佐证。
10. **别用 `prctl` 设 `oom_score_adj`。** 实测它返回 0 却什么都不做；写
    `/proc/self/oom_score_adj` 才有效。所以 `STUDIO_OOM_SCORE_ADJ` 的实现是写 proc
    文件 + 回读验证（skill-service §13.2）。
11. **指标端口也在 NetworkPolicy 的管辖下。** studio 的 `/metrics` 监听 `9090`
    （`STUDIO_METRICS_PORT`，设成 `0` 关闭）。`studio-networkpolicy.yaml` 里
    **必须**有一条放行 9090 的 ingress 规则给抓取器——不放行的话，指标端点在容器
    里好好地跑着，抓取器却连不上。清单里那条规则的命名空间/标签是**占位值**，
    按你的监控栈改。
12. **`isolated` 档没有容器运行时。** 清单里 `STUDIO_ISOLATED_SKILL_CODES` 默认留空
    ——**填了技能码，那些技能会拿到明确的 `FAILED_PRECONDITION`**（而不是悄悄退回
    `fast` 档）。参考实现不提供一次性容器执行器：起容器要挂 Docker socket，与
    「无宿主挂载」的加固模型直接冲突（skill-service §2.4、§8.4）。

### 指标端点

```bash
# 在 studio 容器里抓一次指标
docker exec studio python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9090/metrics').read().decode())"
```

输出是 **Prometheus 文本 exposition 格式**（`text/plain; version=0.0.4`），
指标清单见 skill-service §12.1。**没有引第三方库**——studio 镜像只装 grpcio +
protobuf 是有意的原则（容器里跑的是模型指定的代码，依赖越少越好）。
