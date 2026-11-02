"""studio：技能脚本的受限执行服务（skill-service §3）。

它是**独立的**服务：只认「一段脚本 + 一组参数」，不认识用户、不认识技能、不
认识 web_agent。与 web_agent 之间只有一个 gRPC 契约（proto 文件在仓库里只有
一份：``proto/webagent/studio/v1/studio.proto``）。

用法::

    python -m services.studio            # 监听 0.0.0.0:9000
    STUDIO_PORT=9001 python -m services.studio
"""

from __future__ import annotations

import logging

from services.studio.config import StudioConfig
from services.studio.server import StudioService

logger = logging.getLogger(__name__)

__all__ = ["StudioConfig", "StudioService", "serve"]


async def serve(config: StudioConfig | None = None) -> None:
    """启动 gRPC 服务并一直跑下去。

    Args:
        config: 服务配置；不给就从环境变量读。
    """
    import grpc

    from webagent.studio.v1 import studio_pb2_grpc

    cfg = config or StudioConfig.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    service = StudioService(cfg)
    server = grpc.aio.server()
    studio_pb2_grpc.add_StudioServicer_to_server(service, server)
    address = f"{cfg.host}:{cfg.port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info(
        "studio 已启动：%s（版本 %s，运行时 %s，并发上限 %s，隔离档 %s）",
        address,
        cfg.version,
        "、".join(cfg.languages) or "（无）",
        cfg.max_concurrency,
        cfg.profile,
    )
    try:
        await server.wait_for_termination()
    finally:
        # 优雅停机：先摘 readiness，再 drain（§13.5）。
        service.set_ready(False)
        await server.stop(grace=5)
        logger.info("studio 已关闭")
