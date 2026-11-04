# gRPC Proto 契约

仓库中的 proto 契约唯一来源：

```text
proto/webagent/studio/v1/studio.proto
```

`webagent/studio/v1/` 是由该文件生成的 Python 桩代码，不应手工修改。

## 生成与检查

```bash
python scripts/gen_proto.py
python scripts/check_proto_contract.py
pytest -q tests/test_proto_contract.py
```

一致性检查还会比对以下文档中的 proto 片段：

- `app/design/v4-tools-skills.md` §22.8.3
- `services/studio/design/v4-current.md` §5

修改契约时，三处内容必须逐字一致，并重新生成桩代码。

## 相关文档

- [studio 服务](../services/studio/README.md)
- [studio 设计目录](../services/studio/design/README.md)
- [脚本说明](../scripts/README.md)
