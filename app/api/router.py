"""路由注册。"""

from fastapi import APIRouter

from app.api.routes import (
    agent,
    admin_user,
    auth,
    catalog,
    conversation,
    health,
    llm_endpoint,
    mcp,
    oauth_provider,
    run,
    skill,
    tool,
    tool_audit,
    workflow,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(catalog.router)
api_router.include_router(admin_user.router)
api_router.include_router(oauth_provider.router)
api_router.include_router(agent.router)
api_router.include_router(llm_endpoint.router)
api_router.include_router(tool.router)
api_router.include_router(tool_audit.router)
api_router.include_router(workflow.router)
api_router.include_router(conversation.router)
api_router.include_router(run.router)
api_router.include_router(skill.router)
api_router.include_router(mcp.router)

__all__ = ["api_router"]
