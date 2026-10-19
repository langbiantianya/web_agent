"""Run 路由：SSE 流、详情、取消（设计文档 §5 / §8）。"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from app.api.deps import DbSession, ResumeFrom
from app.config.settings import settings
from app.core.errors import ConflictError, GoneError, NotFoundError
from app.core.orchestrator import get_orchestrator
from app.core.run_hub import RunEventDTO, get_hub
from app.core.sse_writer import get_sse_stream
from app.core.ticket import verify_ticket
from app.models.enums import RunStatus
from app.repo.message_repo import message_repo
from app.repo.run_approval_repo import run_approval_repo
from app.repo.run_event_repo import run_event_repo
from app.repo.run_repo import run_repo
from app.schemas.conversation import (
    ApprovalDecisionIn,
    ApprovalDecisionOut,
    ApprovalOut,
    CancelOut,
    MessageOut,
    RunDetailOut,
    RunStepOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _to_sse(evt: RunEventDTO) -> dict:
    """SSE 帧：id=seq（续传锚点），event=类型，data=JSON。"""
    return {
        "id": str(evt.seq),
        "event": evt.event_type,
        "data": json.dumps(evt.payload, ensure_ascii=False),
    }


@router.get("/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: int, db: DbSession) -> RunDetailOut:
    """run 详情：run 主记录 + 每个节点一步的执行情况（steps，按开始顺序）。

    面向「流已经结束、想复盘整体执行链路」的场景（比如某节点重试了几次、失败在
    哪一步）；**增量吐字过程仍然只能看 SSE**，这里拿到的是快照不是流。
    run 不存在返回 404。
    """
    run = await run_repo.get_by_id(db, run_id)
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")
    steps = await run_repo.list_steps(db, run_id)
    detail = RunDetailOut.model_validate(run)
    detail.steps = [RunStepOut.model_validate(s) for s in steps]
    return detail


@router.get("/{run_id}/messages", response_model=list[MessageOut])
async def get_run_messages(run_id: int, db: DbSession) -> list[MessageOut]:
    """run 的最终消息。事件被归档清理（410 场景）后，前端靠这个接口拿最终内容。"""
    run = await run_repo.get_by_id(db, run_id)
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")
    msgs = await message_repo.list_by_run(db, run_id)
    return [MessageOut.model_validate(m) for m in msgs]


@router.post("/{run_id}/cancel", response_model=CancelOut)
async def cancel_run(run_id: int, db: DbSession) -> CancelOut:
    """取消生成：置取消标志，执行中的 LLM 流在下一个 chunk 收尾。"""
    run = await run_repo.get_by_id(db, run_id)
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")

    if run.is_terminal:
        return CancelOut(run_id=run_id, status=run.status, cancelled=False)

    if not await get_orchestrator().cancel(run_id):
        # run 不在本进程运行（例如刚重启），直接把状态收敛掉
        await run_repo.finish(
            db,
            run_id,
            RunStatus.CANCELLED,
            run.last_seq or 0,
            "取消请求：run 已不在运行中",
        )
        return CancelOut(run_id=run_id, status=RunStatus.CANCELLED.value, cancelled=True)

    return CancelOut(run_id=run_id, status="cancelling", cancelled=True)


@router.get("/{run_id}/approval", response_model=ApprovalOut | None)
async def get_pending_approval(run_id: int, db: DbSession) -> ApprovalOut | None:
    """当前待审批项；没有就返回 null（HTTP 200 + body null）。

    页面刷新后靠它重建审批卡片——SSE 断了不代表审批消失了，run 还挂着等决策。
    """
    run = await run_repo.get_by_id(db, run_id)
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")

    approval = await run_approval_repo.get_pending(db, run_id)
    if approval is None:
        return None
    return ApprovalOut.model_validate(approval)


@router.post("/{run_id}/approval", response_model=ApprovalDecisionOut)
async def submit_approval(
    run_id: int, body: ApprovalDecisionIn, db: DbSession
) -> ApprovalDecisionOut:
    """提交审批决策：批准则继续跑，驳回则终止本次 run。

    决策落库后**立刻在后台恢复执行**，接口不等后续节点跑完——后面可能还有好几个
    节点要跑好几分钟。

    Args:
        run_id: 要决策的 run。
        body: 决策内容（批准与否、意见、决策人）。
        db: 请求级数据库会话。

    Raises:
        NotFoundError: run 不存在。
        ConflictError: run 不在等待审批状态，或该审批已被决策过。
    """
    run = await run_repo.get_by_id(db, run_id)
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")

    if run.status != RunStatus.WAITING.value:
        raise ConflictError(f"run 当前状态是 {run.status}，不在等待审批")

    approval = await run_approval_repo.get_pending(db, run_id)
    if approval is None:
        raise ConflictError("该 run 没有待审批项（可能已被决策过）")

    decided = await run_approval_repo.decide(
        db, approval.id, body.approved, body.comment, body.decidedBy
    )
    if decided is None or decided.status == "pending":
        # 并发提交：另一次请求已经决策过了
        raise ConflictError("该审批已被决策过")

    # 后台恢复执行：批准 → 接着往下跑；驳回 → 图节点抛 RunRejected，run 终止
    decision = {
        "approved": body.approved,
        "comment": body.comment,
        "decidedBy": body.decidedBy,
    }
    get_orchestrator().spawn_resume(run_id, decision)

    return ApprovalDecisionOut(
        run_id=run_id,
        approval_id=approval.id,
        approved=body.approved,
        status=decided.status,
        comment=decided.comment,
        decided_by=decided.decided_by,
    )


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: int,
    resume_from: ResumeFrom,
    db: DbSession,
    ticket: Annotated[str, Query(description="由 /api/auth/sse-ticket 签发的短期票据")],
) -> EventSourceResponse:
    """SSE 事件流：回放 + 实时合并，支持断线续传。

    鉴权走 ``ticket`` query——EventSource 无法自定义请求头（设计文档 §5.1）。

    续传语义（设计文档 §5.4）：
    - ``Last-Event-ID`` 头优先，回退到 ``last_seq`` query；
    - 先订阅再回放，按 ``seq > max_sent`` 去重合并，不漏字、不重复、不乱序；
    - 心跳由 EventSourceResponse 以 ``: ping`` 注释行发送，不占 seq、不落库。
    """
    verify_ticket(ticket, run_id=run_id)

    run = await run_repo.get_by_id(db, run_id)
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")

    # 410：run 已结束且事件被归档清理，无法回放 → 直接把最终消息给前端
    if run.is_terminal and (run.last_seq or 0) > 0:
        first_seq = await run_event_repo.first_seq(db, run_id)
        if first_seq is None:
            raise GoneError(
                "run 事件已归档清理，无法回放；请读取该 run 的最终消息",
                data={"runId": run_id, "messagesUrl": f"/api/runs/{run_id}/messages"},
            )

    sse_stream = get_sse_stream()
    logger.info(
        "SSE 连接建立 run=%s resume_from=%s active=%s",
        run_id, resume_from, get_hub().is_active(run_id),
    )

    async def event_generator():
        """把领域事件转成 SSE 帧逐条吐出（``SSEStream`` 的薄适配层）。

        这里只负责「调流 + 序列化」，订阅/回放/按 seq 去重合并都在
        :class:`SSEStream` 内，心跳由 ``EventSourceResponse`` 的 ``ping``
        参数以 ``: ping`` 注释行发出——都不在本函数，别在这里重复实现。

        关键：**客户端断开不是错误路径**。浏览器关页面或网络抖动时
        sse_starlette 会取消这个生成器，走 except 分支只记一条 info 日志；
        run 由独立的 asyncio.Task 驱动，断流不影响后台生成与事件落库，
        用户重连后凭 last_seq 续上即可（设计文档 §9）。
        """
        try:
            async for evt in sse_stream.stream(run_id, resume_from):
                yield _to_sse(evt)
        except Exception:  # noqa: BLE001
            # 客户端断开属正常路径（sse_starlette 会取消生成器），
            # run 不受影响，继续在后台生成并写库。
            logger.info("run %s 的 SSE 连接结束", run_id, exc_info=True)

    return EventSourceResponse(
        event_generator(),
        ping=settings.sse_ping_interval,
        headers={
            "Cache-Control": "no-cache",
            # Nginx 关闭缓冲，否则吐字会被攒住才一次性下发
            "X-Accel-Buffering": "no",
        },
    )
