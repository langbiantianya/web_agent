"""Workflow 编排定义路由（设计文档 §8）。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.cache import workflow_cache
from app.core.errors import ConflictError, NotFoundError
from app.repo.workflow_repo import workflow_repo
from app.schemas.workflow import WorkflowCreate, WorkflowOut, WorkflowUpdate

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(payload: WorkflowCreate, db: DbSession) -> WorkflowOut:
    """创建 Workflow。definition 已由 Pydantic 校验过图结构，非法定义在这里就被拒。"""
    if await workflow_repo.get_by_code(db, payload.code):
        raise ConflictError(f"Workflow code 已存在：{payload.code}")
    wf = await workflow_repo.create(
        db,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        definition=payload.definition.model_dump(by_alias=True, exclude_none=True),
        enabled=1 if payload.enabled else 0,
    )
    workflow_cache.put(wf)
    return WorkflowOut.model_validate(wf)


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(
    db: DbSession,
    only_enabled: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[WorkflowOut]:
    """Workflow 编排列表。

    ``only_enabled=true`` 只返回可用编排——这也是新建会话时唯一能被绑定的集合。
    分页 limit/offset，响应不含总数。definition 原文随列表一起返回，
    编排规模大时列表接口会偏重。
    """
    items = await workflow_repo.list(db, only_enabled=only_enabled, limit=limit, offset=offset)
    return [WorkflowOut.model_validate(w) for w in items]


@router.get("/{code}", response_model=WorkflowOut)
async def get_workflow(code: str, db: DbSession) -> WorkflowOut:
    """按 code 查询 Workflow 编排定义，不存在返回 404。

    返回的 definition 即「节点执行顺序 + 每个节点绑定哪个 Agent」，可直接喂给
    前端渲染成流程图。注意 code 是业务主键，不是自增 id。
    """
    wf = await workflow_repo.get_by_code(db, code)
    if wf is None:
        raise NotFoundError(f"Workflow 不存在：{code}")
    return WorkflowOut.model_validate(wf)


@router.put("/{code}", response_model=WorkflowOut)
async def update_workflow(code: str, payload: WorkflowUpdate, db: DbSession) -> WorkflowOut:
    """更新 Workflow 编排定义，不存在返回 404。

    两个要点：
    - **局部更新**：用 ``exclude_unset``，只写请求里显式带上的字段，没传的保持原值；
    - **改动不影响在跑的 run**：run 启动时已把 definition 加载成快照执行到内存，
      这里的修改从下一个 run 才开始生效。

    ``definition`` 落库前会再过一次 ``WorkflowDefinition`` 校验，非法图结构当场拒绝。
    与 Agent 不同，Workflow 没有 version 概念，但同样会同步刷新缓存。
    """
    fields = payload.model_dump(exclude_unset=True)
    if "definition" in fields and payload.definition is not None:
        # 必须从 ``payload.definition``（模型对象）重新 dump，不能用
        # ``fields["definition"]``：上面那行 model_dump() 已经把嵌套模型递归转成
        # 普通 dict 了，再对 dict 调 ``.model_dump()`` 必然 AttributeError → 500。
        # 这里需要重新 dump 只是为了拿到 by_alias / exclude_none 的效果——
        # 外层那次 dump 用的都是默认参数，不做这两件事。
        fields["definition"] = payload.definition.model_dump(
            by_alias=True, exclude_none=True
        )
    if "enabled" in fields and fields["enabled"] is not None:
        fields["enabled"] = 1 if fields["enabled"] else 0
    wf = await workflow_repo.update(db, code, fields)
    if wf is None:
        raise NotFoundError(f"Workflow 不存在：{code}")
    workflow_cache.put(wf)
    return WorkflowOut.model_validate(wf)


@router.delete("/{code}", status_code=204)
async def delete_workflow(code: str, db: DbSession) -> None:
    """删除 Workflow 编排定义（204），不存在返回 404。

    这是**硬删除**：已存在的会话仍然指向它，但后续发消息会以
    「Workflow 不存在或已停用」被拒（409）。只是临时下线请改用 PUT
    ``enabled=false``——那样老会话还能继续跑。
    """
    if not await workflow_repo.delete(db, code):
        raise NotFoundError(f"Workflow 不存在：{code}")
    workflow_cache.clear()
