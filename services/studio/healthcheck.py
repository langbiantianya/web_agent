#!/usr/bin/env python3
"""studio 的探针命令行工具（skill-service §13.3）。

## 为什么使用独立的 Health 探针

标准 gRPC 健康检查协议是 ``grpc.health.v1.Health/Check``。studio 实现的是
契约里自己的 ``Health`` 方法
（``webagent.studio.v1.Studio/Health``），字段是 ``ready / version /
language_info / max_concurrency / profile``，跟标准协议对不上。

硬套标准协议要引入 ``grpcio-health-checking`` 并多维护一份 servicer；用
``exec`` 探针调**我们自己的** Health 更直白，也没有多一个依赖。

## 用法

```bash
python -m services.studio.healthcheck --addr 127.0.0.1:9000
python -m services.studio.healthcheck --addr 127.0.0.1:9000 --require-ready
```

退出码：0 = 通过；1 = 没通过（探针会当成失败）。

``--require-ready`` 给 readiness 探针用：**连得上但没就绪**（队列满、
正在优雅停机）要判成失败，那样这个实例才不会继续接流量。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from webagent.studio.v1 import studio_pb2, studio_pb2_grpc


async def probe(addr: str, require_ready: bool, timeout: float = 3.0) -> int:
    """探一次活。

    Args:
        addr: studio 地址。
        require_ready: 是否要求 ``ready=true``。
        timeout: 超时（秒）。

    Returns:
        退出码：0 通过，1 没通过。
    """
    import grpc

    channel = grpc.aio.insecure_channel(addr)
    try:
        stub = studio_pb2_grpc.StudioStub(channel)
        response = await stub.Health(studio_pb2.HealthRequest(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 探针里什么错都要能变成退出码
        print(f"studio 探针失败（{addr}）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await channel.close()

    if require_ready and not response.ready:
        print(
            f"studio 未就绪（{addr}）：profile={response.profile}", file=sys.stderr
        )
        return 1
    print(
        f"studio ok：version={response.version} "
        f"ready={response.ready} concurrency={response.max_concurrency}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    Args:
        argv: 参数列表；不给就取 ``sys.argv[1:]``。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="studio 健康探针")
    parser.add_argument("--addr", default="127.0.0.1:9000", help="studio 地址")
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="要求 ready=true（readiness 探针用）",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="超时（秒）")
    args = parser.parse_args(argv)
    return asyncio.run(probe(args.addr, args.require_ready, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
