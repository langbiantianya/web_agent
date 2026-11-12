"""Run 路由：SSE 流、详情、取消（设计文档 §5 / §8）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

from app.api.deps import CurrentUserDep, DbSession, ResumeFrom
from app.config.settings import settings
from app.core.errors import ConflictError, GoneError, NotFoundError
from app.core.orchestrator import get_orchestrator
from app.core.run_hub import RunEventDTO, get_hub
from app.core.sse_writer import get_sse_stream
from app.core.ticket import verify_ticket
from app.models.enums import EventType, RunStatus
from app.repo.message_repo import message_repo
from app.repo.run_approval_repo import run_approval_repo
from app.repo.run_event_repo import run_event_repo
from app.repo.run_repo import run_repo
from app.schemas.conversation import (
    ApprovalDecisionIn,
    ApprovalDecisionOut,
    ApprovalOut,
    ApprovalVoteOut,
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
async def get_run(
    run_id: int, db: DbSession, user: CurrentUserDep
) -> RunDetailOut:
    """run 详情：run 主记录 + 每个节点一步的执行情况（steps，按开始顺序）。

    面向「流已经结束、想复盘整体执行链路」的场景（比如某节点重试了几次、失败在
    哪一步）；**增量吐字过程仍然只能看 SSE**，这里拿到的是快照不是流。
    run 不存在返回 404。
    """
    run = await run_repo.get_for_user(
        db, run_id, user.tenant_id, user.user_id
    )
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")
    steps = await run_repo.list_steps(db, run_id)
    detail = RunDetailOut.model_validate(run)
    detail.steps = [RunStepOut.model_validate(s) for s in steps]
    return detail


@router.get("/{run_id}/messages", response_model=list[MessageOut])
async def get_run_messages(
    run_id: int, db: DbSession, user: CurrentUserDep
) -> list[MessageOut]:
    """run 的最终消息。事件被归档清理（410 场景）后，前端靠这个接口拿最终内容。"""
    run = await run_repo.get_for_user(
        db, run_id, user.tenant_id, user.user_id
    )
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")
    msgs = await message_repo.list_by_run(db, run_id)
    return [MessageOut.model_validate(m) for m in msgs]


@router.post("/{run_id}/cancel", response_model=CancelOut)
async def cancel_run(
    run_id: int, db: DbSession, user: CurrentUserDep
) -> CancelOut:
    """取消生成：置取消标志，执行中的 LLM 流在下一个 chunk 收尾。"""
    run = await run_repo.get_for_user(
        db, run_id, user.tenant_id, user.user_id
    )
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
async def get_pending_approval(
    run_id: int, db: DbSession, user: CurrentUserDep
) -> ApprovalOut | None:
    """当前待审批项；没有就返回 null（HTTP 200 + body null）。

    页面刷新后靠它重建审批卡片——SSE 断了不代表审批消失了，run 还挂着等决策。
    """
    run = await run_repo.get_for_user(
        db, run_id, user.tenant_id, user.user_id
    )
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")

    approval = await run_approval_repo.get_pending(db, run_id)
    if approval is None:
        return None
    return ApprovalOut.model_validate(approval)


async def _await_waiting(
    db,
    run_id: int,
    tenant_id: str,
    user_id: str,
    timeout: float = 5.0,
):
    """等 run 的状态落成 ``waiting``。

    编排器暂停在审批节点时的顺序是「建审批记录 → emit 事件 → 置状态」，
    中间只有几毫秒。客户端收到 ``approval_required`` 事件就可能立刻点
    批准，那一刻状态往往还没落上——这里把它等过去。

    **每次轮询前都要 rollback**：MySQL 默认的 REPEATABLE READ 会让同一
    个事务里反复读到**同一个快照**，不换掉事务就永远看不到状态变化。

    Args:
        db: 请求级数据库会话。
        run_id: run id。
        tenant_id: 租户。
        user_id: 用户 id。
        timeout: 最长等待秒数。

    Returns:
        最后一次读到的 run（等不到 waiting 就返回它，让调用方自己判断）。
    """
    deadline = time.monotonic() + timeout
    while True:
        await db.rollback()
        run = await run_repo.get_for_user(db, run_id, tenant_id, user_id)
        if run is None:
            return None
        if run.status == RunStatus.WAITING.value:
            return run
        if time.monotonic() >= deadline:
            return run
        await asyncio.sleep(0.05)


@router.post("/{run_id}/approval", response_model=ApprovalDecisionOut)
async def submit_approval(
    run_id: int,
    body: ApprovalDecisionIn,
    db: DbSession,
    user: CurrentUserDep,
) -> ApprovalDecisionOut:
    """提交审批决策：批准则继续跑，驳回则终止本次 run。

    决策落库后**立刻在后台恢复执行**，接口不等后续节点跑完——后面可能还有好几个
    节点要跑好几分钟。

    Args:
        run_id: 要决策的 run。
        body: 决策内容（批准与否、意见）。
        db: 请求级数据库会话。

    Raises:
        NotFoundError: run 不存在。
        ConflictError: run 不在等待审批状态，或该审批已被决策过。
    """
    run = await run_repo.get_for_user(
        db, run_id, user.tenant_id, user.user_id
    )
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")

    # **竞态**：编排器 `_pause` 的顺序是「先建审批记录 → 再 emit
    # approval_required → 最后把 run 置为 waiting」。客户端看到事件就可能
    # 立刻点批准，而那一刻 run 的状态往往还是 running——直接判 409 会让
    # 「手快点批准」变成一次失败（实测能稳定复现）。
    #
    # 这里的处理是**等编排器把状态落上**，而不是让人重试：
    # run 的 status 与 last_seq 是同一条 UPDATE 写的，所以「状态变成
    # waiting」就意味着 seq 锚点也已经写准——恢复执行读到的 start_seq
    # 不会退回旧值（那会与 run_event 的 uk_run_seq 撞车）。
    #
    # 只有「压根没有待审批记录」才直接拒：那说明这个 run 真的不在审批点上。
    approval = await run_approval_repo.get_pending(db, run_id)
    if approval is None:
        raise ConflictError(
            f"run 当前状态是 {run.status}，没有待审批项（可能已被决策过）"
        )
    if run.status != RunStatus.WAITING.value:
        run = await _await_waiting(
            db, run_id, user.tenant_id, user.user_id, timeout=5.0
        )
        if run is None:
            raise NotFoundError(f"run 不存在：{run_id}")
        if run.status != RunStatus.WAITING.value:
            raise ConflictError(f"run 当前状态是 {run.status}，不在等待审批")
        # 等的这几毫秒里可能被别人批了：重新取一次待审批项。
        # （**必须重新取**：等待过程里 rollback 过，之前那个 ORM 对象已经
        # 过期，再去读它的字段会触发同步 IO，在异步会话里直接炸。）
        approval = await run_approval_repo.get_pending(db, run_id)
        if approval is None:
            raise ConflictError("该 run 没有待审批项（可能已被决策过）")

    # 用户审批人身份只来自当前会话，请求体不能指定其他人。
    decided_by = user.username
    decided = await run_approval_repo.decide(
        db, approval.id, body.approved, body.comment, decided_by
    )
    if decided is None:
        # 并发提交：条件更新没命中，说明另一个请求已经决策过了。
        # 这里必须拦住——放行就会拉起第二个恢复任务，两个任务并发驱动同一条
        # LangGraph thread，审批后的节点会跑两遍。
        raise ConflictError("该审批已被决策过")

    await get_hub().emit(
        run_id,
        EventType.APPROVAL_VOTE,
        {
            "node": approval.node_id,
            "approved": body.approved,
            "decidedBy": decided_by,
            "approvedCount": decided.approved_count,
            "requiredApprovals": decided.required_approvals,
            "status": decided.status,
        },
    )

    # 只有会签收口后才恢复图。未达到人数的票只更新审批卡片。
    if decided.status == "pending":
        return ApprovalDecisionOut(
            run_id=run_id,
            approval_id=approval.id,
            approved=False,
            status="pending",
            comment=decided.comment,
            decided_by=decided.decided_by,
            pending=True,
            approved_count=decided.approved_count,
            required_approvals=decided.required_approvals,
        )

    decision = {
        "approved": body.approved,
        "comment": body.comment,
        "decidedBy": decided_by,
    }
    get_orchestrator().spawn_resume(run_id, decision)

    return ApprovalDecisionOut(
        run_id=run_id,
        approval_id=approval.id,
        approved=body.approved,
        status=decided.status,
        comment=decided.comment,
        decided_by=decided.decided_by,
        approved_count=decided.approved_count,
        required_approvals=decided.required_approvals,
    )


@router.get(
    "/{run_id}/approval/{approval_id}/votes",
    response_model=list[ApprovalVoteOut],
)
async def list_approval_votes(
    run_id: int,
    approval_id: int,
    db: DbSession,
    user: CurrentUserDep,
) -> list[ApprovalVoteOut]:
    """列出一张会签单的投票记录。"""
    run = await run_repo.get_for_user(
        db, run_id, user.tenant_id, user.user_id
    )
    if run is None:
        raise NotFoundError(f"run 不存在：{run_id}")
    approval = await run_approval_repo.get_by_id(db, approval_id)
    if approval is None or approval.run_id != run_id:
        raise NotFoundError(f"审批不存在：{approval_id}")
    rows = await run_approval_repo.list_votes(db, approval_id)
    return [
        ApprovalVoteOut(
            id=row.id,
            approval_id=row.approval_id,
            decided_by=row.decided_by,
            approved=bool(row.approved),
            comment=row.comment,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: int,
    resume_from: ResumeFrom,
    db: DbSession,
    user: CurrentUserDep,
    ticket: Annotated[str, Query(description="由 /api/auth/sse-ticket 签发的短期票据")],
) -> EventSourceResponse:
    """SSE 事件流：回放 + 实时合并，支持断线续传。

    鉴权走 ``ticket`` query——EventSource 无法自定义请求头（设计文档 §5.1）。

    续传语义（设计文档 §5.4）：
    - ``Last-Event-ID`` 头优先，回退到 ``last_seq`` query；
    - 先订阅再回放，按 ``seq > max_sent`` 去重合并，不漏字、不重复、不乱序；
    - 心跳由 EventSourceResponse 以 ``: ping`` 注释行发送，不占 seq、不落库。
    """
    ticket_data = verify_ticket(ticket, run_id=run_id)
    if (
        ticket_data.get("uid") != user.user_id
        or ticket_data.get("tenant") != user.tenant_id
    ):
        raise NotFoundError(f"run 不存在：{run_id}")

    run = await run_repo.get_for_user(
        db, run_id, user.tenant_id, user.user_id
    )
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

    # **在返回流之前就把事务放掉**。
    #
    # FastAPI 的 yield 依赖（这里是 get_db）是在**响应发完之后**才关闭的，
    # 而 SSE 响应会一直开着（几分钟甚至更久）。于是这个请求的 DB 事务会跟着
    # 整条流一直挂着：一条连接被长期占着，一个读快照长期不释放。
    #
    # 后果有两个，都不好看：
    # - SQLite 的 WAL 只能在**没有活跃读快照**时做 checkpoint。流一直开着，
    #   WAL 文件就一直涨；
    # - 事务一旦被别处改成「一上来就拿写锁」的模式（SQLite 的
    #   BEGIN IMMEDIATE 配方就是），一条开着的流就等于一把长期占着写锁的钥匙。
    #
    # 这里要的数据（run 状态、首 seq）上面已经取完了，回滚一个只读事务不丢
    # 任何东西。回滚之后连接立刻归还，WAL 也能正常 checkpoint。
    await db.rollback()

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
            # ticket 与登录会话都不是永久凭证。连接最多存活一个 ticket TTL，
            # 到点后发一个控制事件让前端重新换票；新连接会重新校验 session，
            # 因而登出、停用和撤销最多在一个 TTL 内生效。
            async with asyncio.timeout(settings.sse_ticket_ttl):
                async for evt in sse_stream.stream(run_id, resume_from):
                    yield _to_sse(evt)
        except TimeoutError:
            yield {
                "event": "ticket_expired",
                "data": json.dumps(
                    {"reason": "ticket_expired", "runId": run_id},
                    ensure_ascii=False,
                ),
            }
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
