# studio 设计索引

studio 的调用侧设计在主服务的 [V4 设计](../../../app/design/v4-tools-skills.md)，当前服务侧设计在 [v4-current.md](v4-current.md)。

当前部署与隔离实现以 Docker Compose、dispatcher 和一次性 sandbox 为准。取消传播、指标、
技能级配额和镜像 digest 校验已落地；镜像签名、自定义 seccomp 与跨实例配额仍未完成，
统一维护在 [全局 TODO](../../../docs/design/TODO.md) 中。

## 文档边界

| 文档 | 视角 |
| --- | --- |
| [主服务 V4](../../../app/design/v4-tools-skills.md) | web_agent 如何校验、调用和回灌结果 |
| [studio V4](v4-current.md) | 服务协议、执行生命周期、加固、部署和故障模式 |
| [dispatcher 说明](../../dispatcher/README.md) | 只调度、不执行正文的控制面 |
| [sandbox 说明](../../sandbox/README.md) | 一次性容器与控制帧 |
