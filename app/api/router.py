"""路由注册。"""

from fastapi import APIRouter

from app.api.routes import (
    agent,
    auth,
    conversation,
    health,
    mcp,
    run,
    skill,
    tool,
    workflow,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(agent.router)
api_router.include_router(tool.router)
api_router.include_router(workflow.router)
api_router.include_router(conversation.router)
api_router.include_router(run.router)
api_router.include_router(skill.router)
api_router.include_router(mcp.router)

__all__ = ["api_router"]
