"""Cross-instance tenant/skill quota admission in dispatcher."""

from __future__ import annotations

import httpx
import pytest

from services.dispatcher.config import DispatcherConfig
from services.dispatcher.quota import QuotaExceeded, QuotaManager
from services.dispatcher.server import DispatcherServer
from services.sandbox.protocol import SandboxResult, SandboxSpec


class _Backend:
    """Minimal backend for HTTP quota tests."""

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        return SandboxResult(status="completed", exit_code=0)

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def test_quota_limits_tenant_and_skill_and_expires():
    """Both concurrency and active cost budgets are enforced."""
    quota = QuotaManager(
        tenant_quotas={"blue": 1},
        skill_quotas={"report": 2},
        tenant_budgets={"blue": 2},
        skill_budgets={"report": 3},
    )
    lease = quota.acquire(
        tenant_id="blue", skill="report", cost=1.5, lease_ms=10_000
    )
    with pytest.raises(QuotaExceeded):
        quota.acquire(tenant_id="blue", skill="other", cost=1, lease_ms=10_000)
    assert quota.release(lease.lease_id) is True
    assert quota.release(lease.lease_id) is False
    assert quota.active == 0


async def test_dispatcher_quota_http_lease_and_release():
    """The HTTP control plane returns a lease and releases it idempotently."""
    config = DispatcherConfig(
        host="127.0.0.1",
        port=0,
        images=(("default", "img:1"),),
        require_digest=False,
        tenant_quotas=(("blue", 1),),
    )
    server = DispatcherServer(config, _Backend())  # type: ignore[arg-type]
    await server.start()
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{server.port}", timeout=10
    )
    try:
        first = await client.post(
            "/v1/quota/acquire",
            json={"tenant_id": "blue", "skill": "s", "cost": 1, "lease_ms": 10_000},
        )
        assert first.status_code == 200, first.text
        second = await client.post(
            "/v1/quota/acquire",
            json={"tenant_id": "blue", "skill": "s", "cost": 1, "lease_ms": 10_000},
        )
        assert second.status_code == 429
        assert second.json()["error_code"] == "quota_exceeded"
        lease_id = first.json()["lease_id"]
        released = await client.post(
            "/v1/quota/release", json={"lease_id": lease_id}
        )
        assert released.json()["released"] is True
    finally:
        await client.aclose()
        await server.close()
