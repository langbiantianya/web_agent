# 测试

推荐用固定 Python 3.11 的测试镜像运行全量测试，避免宿主解释器与二进制依赖不匹配：

```bash
docker build -f Dockerfile.test -t webagent/tests:local .
docker run --rm --memory=1200m --cpus=1 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /tmp:/tmp webagent/tests:local
```

`.dockerignore` 排除 `.env`、Git、虚拟环境和测试数据库，测试镜像不会携带真实凭据。
测试库按进程号隔离，默认位于临时目录；`conftest.py` 还会按模块清表，因此单进程
全量测试不会把上一个模块的 Agent/Skill 带入下一次应用启动审计。需要 Redis 的用例
自行建立连接。

如果测试容器通过宿主 Docker socket 启动 sibling 沙箱，`/tmp` 必须按相同绝对路径
挂载。Docker daemon 解析 bind mount 时看的是宿主文件系统；只挂 socket 会让沙箱读不到
测试生成的 `payload.json`，表现为 `sandbox_result_lost`。

## 测试层次

| 层次 | 代表目录/文件 | 验证内容 |
| --- | --- | --- |
| 单元 | `test_expr.py`、`test_workflow_def.py`、`test_tools.py` | 表达式、DAG 校验和工具协议 |
| 数据与并发 | `test_db_concurrency.py`、`test_event_writer.py`、`test_sse_resume.py` | seq、批量落库、回放和续传 |
| 多实例 | `test_redis.py`、`test_orchestrator_resilience.py` | Pub/Sub、Stream、租约、取消和降级 |
| API 端到端 | `test_api_e2e.py`、`test_approval_e2e.py` | 真实 uvicorn、假模型、SSE 和审批恢复 |
| Skill/MCP | `test_skill_*.py`、`test_mcp_*.py` | 技能注入、资源读取、MCP 传输和工具调用 |
| studio | `test_studio_*.py` | gRPC、超时、OOM、指标、启动检查和故障注入 |
| 沙箱 | `test_sandbox_*.py`、`test_dispatcher*.py` | 控制帧、固定模板、HTTP 调度和隔离档 |

`tests/fake_llm.py` 提供真实 HTTP 的 OpenAI 兼容假模型，用于覆盖完整网络栈。涉及模型参数选择的用例应模拟真模型可能犯的错误，避免测试假设代替产品行为。

## 文档与契约检查

```bash
python scripts/check_docstrings.py
python scripts/check_proto_contract.py
pytest -q tests/test_proto_contract.py
```

## 相关文档

- [脚本说明](../scripts/README.md)
- [proto 契约](../proto/README.md)
- [性能测试](../scripts/PERFORMANCE.md)
