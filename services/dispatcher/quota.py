"""dispatcher 进程内的跨 studio 实例准入控制。

所有 studio 实例共享同一个 dispatcher 时，这个对象就是集群级配额点：

- ``tenant_quotas`` / ``skill_quotas``：同时在跑的请求数；
- ``tenant_budgets`` / ``skill_budgets``：同时在跑的估算成本单位；
- 租约带过期时间，studio 崩溃未 release 时不会永久占额。

这里刻意不依赖 Redis：dispatcher 本身就是共享控制面。若未来部署多个
dispatcher，必须把本模块的状态迁到共享存储，不能声称多控制面之间一致。
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass


class QuotaExceeded(Exception):
    """租户或技能配额不足。"""

    def __init__(self, scope: str, key: str, limit: float) -> None:
        """记录被触发的配额。"""
        super().__init__(f"{scope} 「{key}」超过配额 {limit:g}")
        self.scope = scope
        self.key = key
        self.limit = limit


@dataclass(slots=True)
class _Lease:
    """一个活动租约。"""

    lease_id: str
    tenant_id: str
    skill: str
    cost: float
    expires_at: float


class QuotaManager:
    """租户/技能并发与成本预算准入器。"""

    def __init__(
        self,
        *,
        tenant_quotas: dict[str, int] | None = None,
        skill_quotas: dict[str, int] | None = None,
        tenant_budgets: dict[str, float] | None = None,
        skill_budgets: dict[str, float] | None = None,
    ) -> None:
        """按部署配置建立配额表。"""
        self.tenant_quotas = tenant_quotas or {}
        self.skill_quotas = skill_quotas or {}
        self.tenant_budgets = tenant_budgets or {}
        self.skill_budgets = skill_budgets or {}
        self._leases: dict[str, _Lease] = {}

    def _purge(self) -> None:
        """清掉已过期租约。"""
        now = time.monotonic()
        for lease_id, lease in list(self._leases.items()):
            if lease.expires_at <= now:
                self._leases.pop(lease_id, None)

    def _matching(self, *, tenant_id: str = "", skill: str = "") -> list[_Lease]:
        """取与过滤条件匹配的活动租约。"""
        return [
            lease
            for lease in self._leases.values()
            if (not tenant_id or lease.tenant_id == tenant_id)
            and (not skill or lease.skill == skill)
        ]

    def acquire(
        self,
        *,
        tenant_id: str,
        skill: str,
        cost: float,
        lease_ms: int,
    ) -> _Lease:
        """申请一个租约。

        Raises:
            QuotaExceeded: 任一适用范围达到上限。
        """
        self._purge()
        tenant = (tenant_id or "default")[:64]
        skill_code = (skill or "unknown")[:64]
        amount = max(0.001, min(float(cost), 1_000_000.0))

        tenant_limit = self.tenant_quotas.get(tenant)
        if tenant_limit is not None:
            active = len(self._matching(tenant_id=tenant))
            if active >= tenant_limit:
                raise QuotaExceeded("tenant_concurrency", tenant, tenant_limit)
        skill_limit = self.skill_quotas.get(skill_code)
        if skill_limit is not None:
            active = len(self._matching(skill=skill_code))
            if active >= skill_limit:
                raise QuotaExceeded(
                    "skill_concurrency", skill_code, skill_limit
                )

        tenant_budget = self.tenant_budgets.get(tenant)
        if tenant_budget is not None:
            used = sum(
                lease.cost for lease in self._matching(tenant_id=tenant)
            )
            if used + amount > tenant_budget:
                raise QuotaExceeded("tenant_budget", tenant, tenant_budget)
        skill_budget = self.skill_budgets.get(skill_code)
        if skill_budget is not None:
            used = sum(
                lease.cost for lease in self._matching(skill=skill_code)
            )
            if used + amount > skill_budget:
                raise QuotaExceeded("skill_budget", skill_code, skill_budget)

        ttl = max(1.0, min(math.ceil(lease_ms) / 1000.0, 86400.0))
        lease = _Lease(
            lease_id=secrets.token_hex(16),
            tenant_id=tenant,
            skill=skill_code,
            cost=amount,
            expires_at=time.monotonic() + ttl,
        )
        self._leases[lease.lease_id] = lease
        return lease

    def release(self, lease_id: str) -> bool:
        """释放租约；重复释放返回 False。"""
        self._purge()
        return self._leases.pop(lease_id, None) is not None

    @property
    def active(self) -> int:
        """当前活动租约数。"""
        self._purge()
        return len(self._leases)

    @property
    def active_cost(self) -> float:
        """当前活动成本单位。"""
        self._purge()
        return sum(lease.cost for lease in self._leases.values())
