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

独立后台首次启动会自动创建固定 owner `superAdmin`，并把随机密码只在创建成功的
那一次启动日志中输出。需要预先通过邮件/短信通知时，可以在首次启动前手工运行：

```bash
python -m scripts.bootstrap_superadmin --email ops@example.com --phone +8613800138000
```

手工命令生成密码并通过邮件/短信发送。只有受控本机初始化允许 `--stdout`
一次性输出明文；生产轮换和忘记密码流程不得使用。

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

## 行为差分

```bash
python scripts/diff_workflow_def_behavior.py --base a1bdbc8
```

`diff_workflow_def_behavior.py` 从 base commit 反向重建改造前的 `app/core/workflow_def.py`，
与当前文件在同一进程里分别加载，对 35 个显式用例 + 231 个缺陷组合用例跑校验，
逐字比对 pydantic `ValidationError` 的消息。用于证明「图结构校验从「遇错即抛」
改成「一次收集全部问题」之后，给用户看的中文文案一个字都没变。

## 相关文档

- [测试说明](../tests/README.md)
- [性能与容量](PERFORMANCE.md)
- [proto 契约](../proto/README.md)
