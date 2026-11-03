# sandbox 执行容器

sandbox 是 isolated 档使用的一次性容器。它只执行一个脚本，退出后容器被删除，不复用进程、工作目录或本地状态。

## 启动约定

dispatcher 按固定模板创建容器，并通过只读挂载传入请求载荷。容器入口是 `python -m services.sandbox.runner`，它会：

1. 读取并校验控制帧。
2. 在临时目录创建脚本。
3. 根据语言选择 `python`、`bash` 或 `node`。
4. 为子进程设置资源限制和环境变量白名单。
5. 执行脚本并向 stdout 写入带长度与摘要的响应帧。

## 协议

`protocol.py` 定义以下内容：

- `SandboxSpec`：语言、正文、参数、stdin、时限和 nonce。
- `SandboxResult`：退出状态、退出码、stdout、stderr 和摘要。
- 带长度前缀与摘要校验的帧编码、流式组装和解析。

stdout 专用于协议帧，脚本原始 stdout/stderr 会先被捕获，再嵌入结果帧，避免脚本内容破坏控制协议。

## 安全属性

| 属性 | 约定 |
| --- | --- |
| 用户 | 非 root |
| 根文件系统 | 只读 |
| 可写空间 | 仅临时目录 |
| 网络 | 禁用 |
| 能力 | drop all |
| 特权升级 | 禁止 |
| 挂载 | 不挂业务凭据和宿主目录 |
| 生命周期 | 一次执行一个容器，结束后删除 |

## 相关文档

- [dispatcher](../dispatcher/README.md)
- [studio](../studio/README.md)
- [studio 完整设计](../studio/DESIGN.md)
