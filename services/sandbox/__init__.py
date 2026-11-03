"""一次性沙箱：协议、runner 与镜像（skill-service §2.4、§9）。

这个包同时被三处使用：

- 沙箱镜像里的 ``runner``；
- dispatcher（解析控制帧）；
- studio（构造 payload）。

它**不依赖任何第三方库**——沙箱镜像与 studio 镜像都只装最小依赖。
"""
