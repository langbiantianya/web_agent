"""Agent 配置管理路由（设计文档 §8）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.core.crypto import encrypt
from app.core.errors import ConflictError, NotFoundError
from app.core.tools import known_tool_names, normalize_tool_names
from app.repo.agent_repo import agent_repo
from app.schemas.agent import AgentCreate, AgentOut, AgentUpdate

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _to_out(agent) -> AgentOut:
    """ORM ``Agent`` → 对外响应模型（内部转换层，不直接暴露给调用方）。

    这一层同时是**密钥边界**：``api_key_cipher`` 绝不外泄，只暴露
    ``has_api_key`` 布尔位告诉前端「配没配过」；需要替换密钥走 PUT 覆盖，
    没有任何读接口能拿回明文。

    第二个易踩的坑：库表列大多可空，而 ``AgentOut`` 里 description / temperature /
    max_tokens / version 都是**必填非空**类型。这里统一兜默认值（空串 / 0.7 /
    2048 / 1），否则历史数据里一个 NULL 就能让整个列表接口 500。``enabled`` 在库
    里是 tinyint，也是在这里才转成 bool。

    Args:
        agent: ORM ``Agent`` 实例（来自 repo 查询）。

    Returns:
        ``AgentOut``。``tools`` 只保留**已注册**的工具名（V3.2）——读的时候
        宽容，脏数据不至于让整个列表接口 500；``extra_params`` 是 JSON 列，
        原样透传，不在此处做语义解释。
    """
    return AgentOut(
        id=agent.id,
        code=agent.code,
        name=agent.name,
        description=agent.description or "",
        system_prompt=agent.system_prompt,
        model=agent.model,
        base_url=agent.base_url,
        temperature=float(agent.temperature or 0.7),
        max_tokens=agent.max_tokens or 2048,
        tools=known_tool_names(agent.tools),
        extra_params=agent.extra_params,
        enabled=bool(agent.enabled),
        version=agent.version or 1,
        has_api_key=bool(agent.api_key_cipher),
    )


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(payload: AgentCreate, db: DbSession) -> AgentOut:
    """创建 Agent。配置即数据——新增 Agent 无需改代码、无需发版。

    ``tools`` 存的是**工具名**，未注册的名字在这里就拒（400）——配置根本进不了
    库，也就不会有「模型能调、服务端执行不了」的工具。
    """
    if await agent_repo.get_by_code(db, payload.code):
        raise ConflictError(f"Agent code 已存在：{payload.code}")
    agent = await agent_repo.create(
        db,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model=payload.model,
        base_url=payload.base_url,
        api_key_cipher=encrypt(payload.api_key),
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        tools=normalize_tool_names(payload.tools),
        extra_params=payload.extra_params,
        enabled=1 if payload.enabled else 0,
    )
    return _to_out(agent)


@router.get("", response_model=list[AgentOut])
async def list_agents(
    db: DbSession,
    only_enabled: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[AgentOut]:
    """Agent 配置列表。

    ``only_enabled=true`` 时只返回已启用的 Agent（Workflow 实际能引用的那些）。
    分页为 limit/offset（默认 100，上限 500），响应不含总数，需要展示总页数
    时请另走管理端统计。响应中只有 ``has_api_key``，不会回显密钥。
    """
    agents = await agent_repo.list(db, only_enabled=only_enabled, limit=limit, offset=offset)
    return [_to_out(a) for a in agents]


@router.get("/{code}", response_model=AgentOut)
async def get_agent(code: str, db: DbSession) -> AgentOut:
    """按 code 查询单个 Agent 配置，不存在返回 404。

    code 是业务主键（Workflow 节点引用它），不是自增 id。
    """
    agent = await agent_repo.get_by_code(db, code)
    if agent is None:
        raise NotFoundError(f"Agent 不存在：{code}")
    return _to_out(agent)


@router.put("/{code}", response_model=AgentOut)
async def update_agent(code: str, payload: AgentUpdate, db: DbSession) -> AgentOut:
    """更新 Agent：version+1，缓存键随之失效，下个 run 自动用新配置。

    ``tools`` 传了就校验（未注册的工具名 → 400），传 ``null`` 则清空工具。
    """
    fields = payload.model_dump(exclude_unset=True)
    if "api_key" in fields:
        fields["api_key_cipher"] = encrypt(fields.pop("api_key"))
    if "tools" in fields:
        fields["tools"] = normalize_tool_names(fields["tools"])
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    agent = await agent_repo.update(db, code, fields, bump_version=True)
    if agent is None:
        raise NotFoundError(f"Agent 不存在：{code}")
    return _to_out(agent)


@router.delete("/{code}", status_code=204)
async def delete_agent(code: str, db: DbSession) -> None:
    """删除 Agent。若已被 workflow 引用，建议改用 PUT enabled=false 停用。"""
    ok = await agent_repo.delete(db, code)
    if not ok:
        raise NotFoundError(f"Agent 不存在：{code}")
