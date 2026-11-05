"""Verify cross-instance sandbox quota and real resource accounting.

The script sends a long-running isolated execution through one studio instance,
then sends another request for the same tenant and skill through a second studio
instance. Both instances must share one dispatcher admission plane, so the
second request receives ``RESOURCE_EXHAUSTED`` with ``quota_exceeded``.

Run after starting two isolated studio instances with distinct gRPC addresses::

    python scripts/smoke_shared_quota.py \
      --studio-a 127.0.0.1:9001 --studio-b 127.0.0.1:9002
"""

from __future__ import annotations

import argparse
import asyncio

import grpc

from webagent.studio.v1 import studio_pb2, studio_pb2_grpc


async def _health(stub) -> None:
    """Require both studio instances to report ready."""
    response = await stub.Health(studio_pb2.HealthRequest(), timeout=5)
    if not response.ready:
        raise RuntimeError("studio 未就绪")


async def _exec(stub, *, request_id: str, sleep_seconds: float):
    """Run one isolated request with a bounded duration."""
    return await stub.Exec(
        studio_pb2.ExecRequest(
            skill="quota-smoke",
            tenant_id="quota-smoke-tenant",
            script="scripts/sleep.py",
            language="python",
            content=f"import time\ntime.sleep({sleep_seconds})\n",
            timeout_ms=15_000,
            profile=studio_pb2.EXEC_PROFILE_ISOLATED,
            image="default",
            request_id=request_id,
        ),
        timeout=30,
    )


async def run(studio_a: str, studio_b: str) -> None:
    """Exercise two studio instances against the shared dispatcher."""
    async with (
        grpc.aio.insecure_channel(studio_a) as channel_a,
        grpc.aio.insecure_channel(studio_b) as channel_b,
    ):
        stub_a = studio_pb2_grpc.StudioStub(channel_a)
        stub_b = studio_pb2_grpc.StudioStub(channel_b)
        await asyncio.gather(_health(stub_a), _health(stub_b))

        first = asyncio.create_task(
            _exec(stub_a, request_id="quota-smoke-a", sleep_seconds=5)
        )
        await asyncio.sleep(1)
        try:
            await _exec(stub_b, request_id="quota-smoke-b", sleep_seconds=0)
        except grpc.aio.AioRpcError as exc:
            if (
                exc.code() != grpc.StatusCode.RESOURCE_EXHAUSTED
                or "quota_exceeded" not in exc.details()
            ):
                raise RuntimeError(
                    f"第二个 studio 未返回预期配额错误：{exc.code()} {exc.details()}"
                ) from exc
        else:
            raise RuntimeError("第二个 studio 没有触发共享配额")

        result = await first
        if result.status != studio_pb2.EXEC_STATUS_COMPLETED:
            raise RuntimeError(f"首个沙箱执行失败：{result}")
        if result.cpu_seconds <= 0 or result.peak_memory_bytes <= 0:
            raise RuntimeError(
                "沙箱资源指标为零："
                f"cpu={result.cpu_seconds}, memory={result.peak_memory_bytes}"
            )
        print(
            "shared quota: ok; "
            f"cpu_seconds={result.cpu_seconds:.6f}; "
            f"peak_memory_bytes={result.peak_memory_bytes}"
        )


def main() -> None:
    """Parse addresses and run the smoke test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-a", required=True)
    parser.add_argument("--studio-b", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.studio_a, args.studio_b))


if __name__ == "__main__":
    main()
