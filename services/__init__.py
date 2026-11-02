"""studio 服务（skill-service 的**服务侧**实现）。

与 web_agent 同仓库，但**不依赖它的任何代码**——studio 只认「一段脚本 + 一组
参数」，不认识用户、不认识技能、不认识 web_agent。两边共用的只有一份 proto
契约（``proto/webagent/studio/v1/studio.proto``）与其生成的桩代码。

容器级的加固（只读 rootfs、非 root、禁出网、零凭据）**不在代码里**——那是在编排
平台的容器配置里做的（skill-service §7）。代码里的进程组、ulimit、临时目录
只是第二道。
"""
