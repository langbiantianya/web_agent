"""普通登录用户可读取的 Workflow/Agent 目录。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUserDep, DbSession
from app.repo.agent_repo import agent_repo
from app.repo.workflow_repo import workflow_repo

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("")
async def catalog(db: DbSession, _user: CurrentUserDep) -> dict:
    """返回创建会话所需的公开摘要，不暴露提示词或凭据。"""
    workflows = await workflow_repo.list(db, only_enabled=False, limit=500, offset=0)
    agents = await agent_repo.list(db, only_enabled=False, limit=500, offset=0)
    return {
        "workflows": [
            {
                "code": row.code,
                "name": row.name,
                "enabled": bool(row.enabled),
                "nodes": [
                    node.get("id")
                    for node in (row.definition or {}).get("nodes", [])
                    if isinstance(node, dict) and node.get("id")
                ],
            }
            for row in workflows
        ],
        "agents": [
            {
                "code": row.code,
                "name": row.name,
                "model": row.model,
                "enabled": bool(row.enabled),
            }
            for row in agents
        ],
    }
