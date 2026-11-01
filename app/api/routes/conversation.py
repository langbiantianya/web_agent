"""会话与消息路由（设计文档 §8）。"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.core.errors import ConflictError, NotFoundError, SensitiveContentError
from app.core.orchestrator import get_orchestrator
from app.core.sensitive import screen_text
from app.models.enums import MessageRole, MessageStatus, RunStatus
from app.repo.conversation_repo import conversation_repo
from app.repo.message_repo import message_repo
from app.repo.run_repo import run_repo
from app.repo.workflow_repo import workflow_repo
from app.schemas.conversation import (
    ActiveRunOut,
    ConversationCreate,
    ConversationOut,
    MessageOut,
    SendMessageIn,
    SendMessageOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(payload: ConversationCreate, db: DbSession) -> ConversationOut:
    """创建会话。前端持有 session_id，用于刷新后重新定位会话。"""
    wf = await workflow_repo.get_by_code(db, payload.workflow_code)
    if wf is None:
        raise NotFoundError(f"Workflow 不存在：{payload.workflow_code}")
    if not wf.enabled:
        raise ConflictError(f"Workflow 已停用：{payload.workflow_code}")

    session_id = payload.session_id or uuid.uuid4().hex
    if await conversation_repo.get_by_session_id(db, session_id):
        raise ConflictError(f"session_id 已存在：{session_id}")

    conv = await conversation_repo.create(
        db,
        session_id=session_id,
        workflow_id=wf.id,
        user_id=payload.user_id,
        title=payload.title,
    )
    return ConversationOut.model_validate(conv)


@router.get("/by-session/{session_id}", response_model=ConversationOut)
async def get_conversation_by_session(session_id: str, db: DbSession) -> ConversationOut:
    """按 session_id 查询会话，不存在返回 404。

    session_id 由前端持有（创建时生成或复用），页面刷新后靠它重新定位会话，
    因此它是会话的对外身份，不要用自增 id 替代。拿到会话后再取
    ``active-run`` 与历史消息，即「会话级别恢复」的两步。
    """
    conv = await conversation_repo.get_by_session_id(db, session_id)
    if conv is None:
        raise NotFoundError(f"会话不存在：{session_id}")
    return ConversationOut.model_validate(conv)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    db: DbSession,
    user_id: str = Query("anonymous"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[ConversationOut]:
    """按 user_id 列出会话，用于侧边栏会话列表。

    user_id 缺省为 ``anonymous``：不传就是匿名用户的会话列表，混用会导致查不到
    自己刚建的会话。分页 limit/offset，响应不含总数。
    """
    items = await conversation_repo.list_by_user(db, user_id, limit, offset)
    return [ConversationOut.model_validate(c) for c in items]


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: int,
    db: DbSession,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[MessageOut]:
    """会话历史——「会话级别恢复」的第一层：页面刷新后先把历史拉回来。"""
    conv = await conversation_repo.get_by_id(db, conversation_id)
    if conv is None:
        raise NotFoundError(f"会话不存在：{conversation_id}")
    msgs = await message_repo.list_by_conversation(db, conversation_id, limit, offset)
    return [MessageOut.model_validate(m) for m in msgs]


@router.get("/{conversation_id}/active-run", response_model=ActiveRunOut | None)
async def get_active_run(conversation_id: int, db: DbSession) -> ActiveRunOut | None:
    """进行中的 run——「会话级别恢复」的第二层。

    拿到 runId 后即可用 last_seq=0 走流级别续传，服务端会把历史事件全部回放。
    没有进行中的 run 时返回 null（HTTP 200 + body null）。
    """
    run = await run_repo.get_active_by_conversation(db, conversation_id)
    if run is None:
        return None
    return ActiveRunOut(
        run_id=run.id,
        status=run.status,
        current_node=run.current_node,
        last_seq=run.last_seq or 0,
        stream_url=f"/api/runs/{run.id}/stream",
    )


@router.post("/{conversation_id}/messages", response_model=SendMessageOut, status_code=202)
async def send_message(
    conversation_id: int, payload: SendMessageIn, db: DbSession
) -> SendMessageOut:
    """发送消息：落库用户消息 + 建 run，然后**立即返回**，生成在后台跑。

    同一个会话不允许并发两个 run（409），避免两个 run 交叉写事件流。
    """
    # 敏感词前置过滤（V3.4）：拦在**最前面**。被拦下的消息不进库、不建 run、
    # 不消耗任何 token——拦在后面就等于「先跑完模型再把结果扔掉」，白烧一次
    # 模型调用，还把敏感内容送到了模型那里。
    #
    # **放线程里跑**：判定是纯 CPU，但一条长消息要 20 ms 左右，同步跑在事件
    # 循环里就是整个 worker 停摆——同进程所有 SSE 流、心跳、API 请求一起卡住。
    verdict = await asyncio.to_thread(screen_text, payload.content)
    if verdict is not None and verdict.hit:
        # 只记分数和位置，**不记命中词**：把敏感词写进日志，等于给日志库
        # 造了一份敏感词副本，审计与合规都成了笑话。
        logger.info(
            "提示词被敏感词过滤拦下：分数 %.3f，位置 %d",
            verdict.score,
            verdict.position,
        )
        raise SensitiveContentError(
            "内容包含不被允许的请求，请调整措辞后重试",
            data={"score": verdict.score, "excerpt": verdict.excerpt},
        )

    # 行锁把「查进行中 run → 建 run」串行化，堵住并发窗口
    conv = await conversation_repo.lock_by_id(db, conversation_id)
    if conv is None:
        raise NotFoundError(f"会话不存在：{conversation_id}")

    active = await run_repo.get_active_by_conversation(db, conversation_id)
    if active is not None:
        raise ConflictError(
            f"该会话已有进行中的 run（id={active.id}，status={active.status}）"
        )

    wf = await workflow_repo.get_by_id(db, conv.workflow_id)
    if wf is None or not wf.enabled:
        raise ConflictError("会话绑定的 Workflow 不存在或已停用")

    run = await run_repo.create(
        db,
        conversation_id=conversation_id,
        workflow_id=conv.workflow_id,
        status=RunStatus.PENDING.value,
    )
    await message_repo.add(
        db,
        conversation_id=conversation_id,
        run_id=run.id,
        agent_code=None,
        role=MessageRole.USER.value,
        content=payload.content,
        status=MessageStatus.DONE.value,
    )
    await db.commit()

    # 提交后再起后台任务：此时 run.id 已确定，且不与请求事务耦合
    get_orchestrator().spawn(run.id)

    return SendMessageOut(
        run_id=run.id,
        stream_url=f"/api/runs/{run.id}/stream",
        conversation_id=conversation_id,
        status=run.status,
    )
