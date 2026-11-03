# 测试

运行全量测试：

```bash
pytest -q
```

测试库按进程号隔离，默认位于临时目录，避免并行 pytest 互相抢数据库。`conftest.py` 会强制关闭 Redis，需要 Redis 的用例自行建立连接。

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
