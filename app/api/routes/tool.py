"""内置工具目录（V3.2，设计文档 §17）。

暴露一个只读接口，让后台能列出「这台部署上有哪些工具可用」，而不是让用户在
输入框里凭记忆敲工具名——敲错了要等保存时才被拒。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.tools import TOOL_REGISTRY

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools() -> list[dict]:
    """列出全部内置工具。

    Returns:
        每个工具一条：``name`` / ``description`` / ``parameters``。

    ``parameters`` 是 OpenAI function-calling 的 JSON Schema，前端可以直接拿它
    渲染「这个工具要什么参数」的提示，但**不能**拿它去执行——执行只认名字，
    对应的是服务端注册表里的实现。
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in TOOL_REGISTRY.values()
    ]
