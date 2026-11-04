"""运行期 LLM 接口配置加载与优先级排序。"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.db import SessionLocal
from app.models.agent import Agent
from app.repo.llm_endpoint_repo import llm_endpoint_repo


@dataclass(frozen=True, slots=True)
class LlmEndpointSpec:
    """一次节点执行使用的不可变接口快照。"""

    code: str
    name: str
    base_url: str
    api_key_cipher: str
    priority: int = 100

    @property
    def display_name(self) -> str:
        """用于日志和错误说明，不包含密钥。"""
        return self.name or self.code


def legacy_agent_endpoint(agent: Agent) -> LlmEndpointSpec:
    """把旧版 Agent 内嵌地址转换成单元素回退链。

    新部署以 ``llm_endpoint`` 表为准；保留这条路径是为了让升级前已经存在、
    尚未迁移到独立接口配置的 Agent 仍能运行。
    """
    return LlmEndpointSpec(
        code=f"agent:{agent.code}",
        name=f"{agent.code}（Agent 兼容配置）",
        base_url=agent.base_url.rstrip("/"),
        api_key_cipher=agent.api_key_cipher,
        priority=1_000_000,
    )


async def load_llm_endpoints(agent: Agent) -> tuple[LlmEndpointSpec, ...]:
    """读取当前启用的数据库接口配置。

    数据库一旦存在任何配置，就完全以启用项为准，并按 ``priority`` 从小到大
    尝试；管理员把全部配置停用意味着明确关闭调用，不会偷偷回退到旧 Agent
    地址。只有表里完全没有配置时才回退旧地址。每个节点执行前重新加载，因此
    管理端修改后下一次节点执行立即生效。

    Args:
        agent: 当前节点的 Agent 配置，用于无独立接口配置时兼容回退。

    Returns:
        按尝试顺序排列的接口快照；数据库配置全部停用或没有任何可用地址时为空。
    """
    async with SessionLocal() as db:
        rows = await llm_endpoint_repo.list(db, only_enabled=True, limit=500)
        has_config = await llm_endpoint_repo.has_any(db)
    if rows:
        return tuple(
            LlmEndpointSpec(
                code=row.code,
                name=row.name or row.code,
                base_url=row.base_url.rstrip("/"),
                api_key_cipher=row.api_key_cipher,
                priority=row.priority,
            )
            for row in rows
        )
    if has_config:
        return ()
    if agent.base_url:
        return (legacy_agent_endpoint(agent),)
    return ()
