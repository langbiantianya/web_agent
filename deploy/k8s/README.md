# studio 的部署产物

这三个文件对应 skill-service §7（沙箱加固）、§13（部署与扩缩）：

| 文件 | 内容 |
| --- | --- |
| `studio-deployment.yaml` | Deployment（replicas ≥ 2）+ PodDisruptionBudget |
| `studio-service.yaml` | **headless** Service |
| `studio-networkpolicy.yaml` | NetworkPolicy（入向只放 web_agent，出向全禁） |

镜像在 `services/studio/Dockerfile`。

---

## 本环境无法验证的部分

**这份清单没有在真实 K8s 集群上跑过。** 开发这个特性的机器上没有 Docker，
也没有集群，所以以下几条只做了「对照文档逐条写」，没有实测：

- 镜像能不能构建成功（多阶段构建、node 二进制拷贝、apt 包名）；
- 探针命令在镜像里能不能跑通（`python -m services.studio.healthcheck`）；
- `readOnlyRootFilesystem: true` 下 `/tmp` 的 tmpfs 挂载是否够用；
- NetworkPolicy 是否真的拦住了出向（取决于集群的 CNI）；
- headless + `round_robin` 是否真的把流量摊到了多个 Pod 上。

**已验证的部分**：清单里的每个字段都能对上文档里的对应条目；镜像里的启动命令
`python -m services.studio` 与本地跑通的方式一致；探针脚本 `healthcheck.py` 的
逻辑在本地对着真 studio 验过（连得上、`ready=true` 时退出 0）。

部署前请在目标集群里先跑一遍下面这三条：

```bash
# 1) 镜像构建
docker build -f services/studio/Dockerfile -t webagent/studio:1.0.0 .

# 2) 探针在镜像里能跑通
docker run --rm webagent/studio:1.0.0 \
  python -m services.studio.healthcheck --addr 127.0.0.1:9000

# 3) NetworkPolicy 真的生效（进了 studio Pod 再往外连）
kubectl exec -it deploy/studio -- curl -m 3 https://example.com
```

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
