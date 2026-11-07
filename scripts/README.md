# 开发与运维脚本

`scripts/` 存放演示数据、压测、敏感内容模型训练、proto 生成和文档一致性检查脚本。

`smoke_runtime.py` 用于对已启动服务做低并发真实链路验收：数据库中的 LLM 接口
优先级切换、已发布 Skill 与资源、真实 stdio MCP、以及 MiniMax OpenAI 兼容
接口。凭据只从环境变量读取，不会打印到结果中。

## 数据与启动

```bash
python -m scripts.seed
```

`seed.py` 创建数据库 LLM 接口 `default`、`planner / researcher / writer`
三个 Agent 和 `default_pipeline`。`.env` 只提供首次灌入的接口地址与 Key。

独立后台首次启动还需要创建固定 owner `superAdmin`：

```bash
python -m scripts.bootstrap_superadmin --email ops@example.com --phone +8613800138000
```

密码由服务端随机生成并通过邮件/短信发送。只有受控本机初始化允许
`--stdout` 一次性输出明文；生产轮换和忘记密码流程不得使用。

## 敏感内容模型

```bash
# 训练并导出模型
python -m scripts.train_sensitive

# 使用自定义语料训练
python -m scripts.train_sensitive --data my_corpus.jsonl --out app/core/sensitive_model.npz

# 评测留出集、训练集、仓库文档和阈值曲线
python -m scripts.eval_sensitive --model app/core/sensitive_model.npz
```

训练语料位于 `sensitive_corpus.py`，从未参与训练的留出语料位于 `sensitive_eval_corpus.py`。模型文件读不出来时主服务会启动失败，不做静默放行。

## 压测

```bash
python scripts/bench.py --label saturated --chunk-delay 0
python scripts/bench.py --label realistic --chunk-delay 0.02
```

压测会创建独立临时数据库和真实 uvicorn 进程，并启动 OpenAI 兼容的假模型。完整结果与容量推算见 [性能报告](PERFORMANCE.md)。

## Proto 与文档检查

```bash
python scripts/gen_proto.py
python scripts/check_proto_contract.py
python scripts/check_docstrings.py
```

- `gen_proto.py` 从唯一 proto 文件生成 Python 桩代码。
- `check_proto_contract.py` 校验 proto 与 V4 主设计、studio V4 设计中的片段逐字一致。
- `check_docstrings.py` 扫描 Python 函数并报告缺失 docstring。

新增函数必须写 docstring。参数、返回值和异常语义较复杂时，使用 Google 风格字段说明。

## 相关文档

- [测试说明](../tests/README.md)
- [性能与容量](PERFORMANCE.md)
- [proto 契约](../proto/README.md)
