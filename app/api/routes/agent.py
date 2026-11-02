"""Agent 配置管理路由（设计文档 §8）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.core.crypto import encrypt
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.skill_names import SkillConfigError, known_skill_codes, normalize_skill_codes
from app.core.tools import known_tool_names, normalize_tool_names
from app.repo.agent_repo import agent_repo
from app.repo.skill_repo import skill_repo
from app.schemas.agent import AgentCreate, AgentOut, AgentUpdate
from app.schemas.skill import SkillPromptPreviewOut

router = APIRouter(prefix="/api/agents", tags=["agents"])


async def _skill_code_sets(db) -> tuple[set[str], set[str]]:
    """库里已存在的技能 code 集合与其中已启用的那部分。

    两个集合用途不同：

    - **全部存在的**：读路径用它把 Agent 上残留的失效绑定丢掉——技能被删掉之后，
      那个绑定就该消失，而不是继续显示一个指向空壳的名字；
    - **已启用的**：写路径用它校验——技能正文会进 prompt，而 prompt 就是模型的
      行为，绑一个停用中的技能等于绑了一条不会生效的指令。
    """
    rows = await skill_repo.list(db, limit=1000)
    all_codes = {s.code for s in rows}
    enabled = {s.code for s in rows if s.enabled}
    return all_codes, enabled


def _to_out(agent, known_skills: set[str] | None = None) -> AgentOut:
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
        known_skills: 库里已存在的技能 code 集合，用来过滤失效的绑定。

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
        skills=known_skill_codes(agent.skills, known_skills),
        extra_params=agent.extra_params,
        enabled=bool(agent.enabled),
        version=agent.version or 1,
        has_api_key=bool(agent.api_key_cipher),
    )


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(payload: AgentCreate, db: DbSession) -> AgentOut:
    """创建 Agent。配置即数据——新增 Agent 无需改代码、无需发版。

    ``tools`` 存的是**工具名**，未注册的名字在这里就拒（400）——配置根本进不了
    库，也就不会有「模型能调、服务端执行不了」的工具。``skills`` 同理：绑一个
    库里没有或已停用的技能，在这里就拒。
    """
    if await agent_repo.get_by_code(db, payload.code):
        raise ConflictError(f"Agent code 已存在：{payload.code}")
    all_codes, enabled_codes = await _skill_code_sets(db)
    try:
        skills = normalize_skill_codes(payload.skills, enabled_codes)
    except SkillConfigError as exc:
        raise BadRequestError(str(exc)) from exc
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
        skills=skills,
        extra_params=payload.extra_params,
        enabled=1 if payload.enabled else 0,
    )
    return _to_out(agent, all_codes)


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
    all_codes, _ = await _skill_code_sets(db)
    return [_to_out(a, all_codes) for a in agents]


@router.get("/{code}", response_model=AgentOut)
async def get_agent(code: str, db: DbSession) -> AgentOut:
    """按 code 查询单个 Agent 配置，不存在返回 404。

    code 是业务主键（Workflow 节点引用它），不是自增 id。
    """
    agent = await agent_repo.get_by_code(db, code)
    if agent is None:
        raise NotFoundError(f"Agent 不存在：{code}")
    all_codes, _ = await _skill_code_sets(db)
    return _to_out(agent, all_codes)


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
    if "skills" in fields:
        _, enabled_codes = await _skill_code_sets(db)
        try:
            normalized = normalize_skill_codes(fields["skills"], enabled_codes)
        except SkillConfigError as exc:
            raise BadRequestError(str(exc)) from exc
        # 显式传 null 或 [] 表示「解绑全部」。仓储层的约定是「值为 None 的键跳过」，
        # 所以这里用空列表表达「清空」——空列表不是 None，会被真的写进去。
        fields["skills"] = [] if normalized is None else normalized
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    agent = await agent_repo.update(db, code, fields, bump_version=True)
    if agent is None:
        raise NotFoundError(f"Agent 不存在：{code}")
    all_codes, _ = await _skill_code_sets(db)
    return _to_out(agent, all_codes)


@router.get("/{code}/prompt-preview", response_model=SkillPromptPreviewOut)
async def preview_agent_prompt(code: str, db: DbSession) -> SkillPromptPreviewOut:
    """生效提示词预览：system_prompt + 各技能正文，与实际发给模型的逐字一致。

    存在的意义是「后台看到的」必须等于「模型收到的」：技能正文是**独立的一条**
    system 消息（§22.6），如果它被拼进 ``system_prompt`` 字段，运营在这个
    接口里看到的内容就会和真正发出去的不一样。

    这里复用编排器**同一套**拼装逻辑（``Orchestrator._build_skill_plan``），
    而不是另写一遍——两处各写一遍的话，预览迟早会和实际发出去的不一致。
    """
    agent = await agent_repo.get_by_code(db, code)
    if agent is None:
        raise NotFoundError(f"Agent 不存在：{code}")

    from app.core.orchestrator import get_orchestrator

    plan = await get_orchestrator()._build_skill_plan(agent)
    skills = await skill_repo.list_by_codes(
        db, known_skill_codes(agent.skills) or []
    )
    by_code = {s.code: s for s in skills}
    return SkillPromptPreviewOut(
        agent_code=code,
        system_prompt=agent.system_prompt,
        skills=[
            {
                "code": c,
                "name": by_code[c].name if c in by_code else c,
                "version": by_code[c].version if c in by_code else 0,
                "enabled": bool(by_code[c].enabled) if c in by_code else False,
                "injected": c in plan.codes,
                "body": by_code[c].body if c in by_code else "",
            }
            for c in (known_skill_codes(agent.skills) or [])
        ],
        messages=[
            {"role": "system", "content": agent.system_prompt},
            *plan.messages,
        ],
        skipped=[{"code": c, "reason": r} for c, r in plan.skipped],
    )


@router.delete("/{code}", status_code=204)
async def delete_agent(code: str, db: DbSession) -> None:
    """删除 Agent。若已被 workflow 引用，建议改用 PUT enabled=false 停用。"""
    ok = await agent_repo.delete(db, code)
    if not ok:
        raise NotFoundError(f"Agent 不存在：{code}")
