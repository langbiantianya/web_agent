"""编排器的收尾纪律：任何失败路径都必须收敛成 run 的终态。

这个文件盯的是一组**只在出故障时才看得见**的不变式：

1. 审批恢复（``resume``）失败时，run 必须收敛成终态并发终止事件；
2. 同一个审批只能被决策一次——**并发**提交也不行；
3. 进程退出路径不能把 ``last_seq`` 写成 0；
4. 节点重试的每一次尝试都要留下记录。

这些路径以前各有各的问题，而且症状都一样：run 永远停在 ``running``，SSE 连接
挂死，别的实例的启动恢复还会一直跳过它。
"""

from __future__ import annotations

import asyncio

import pytest

from app.config.db import SessionLocal
from app.core.orchestrator import Orchestrator
from app.core.run_hub import get_hub
from app.models.conversation import Conversation
from app.models.enums import EventType, RunStatus, StepStatus
from app.repo.run_approval_repo import run_approval_repo
from app.repo.run_repo import run_repo

_ids = iter(range(960_000, 999_000))


async def _make_run(status: str = RunStatus.WAITING.value) -> int:
    """建一条 run，返回它的 id。"""
    cid = next(_ids)
    async with SessionLocal() as db:
        db.add(
            Conversation(
                id=cid, session_id=f"s-{cid}", workflow_id=1, user_id="tester"
            )
        )
        await db.commit()
        run = await run_repo.create(
            db,
            conversation_id=cid,
            workflow_id=1,
            status=status,
        )
        await db.commit()
        return run.id


# --------------------------------------------------- 审批恢复失败必须收尾


async def test_resume_failure_finishes_the_run(monkeypatch):
    """恢复执行失败时，run 必须变成 error —— 不能永远停在 running。

    这条盯的是 ``resume`` 的收尾阶梯。以前它只捕 ``RunRejected``，其余异常
    直接逃出后台任务：run 停在 ``running``、不 emit 终止事件（SSE 挂死）、
    租约不释放。
    """
    run_id = await _make_run()
    orch = Orchestrator()

    async def _boom(_run_id: int, _decision: dict) -> None:
        """模拟恢复执行时的失败。"""
        raise RuntimeError("模型端点挂了")

    monkeypatch.setattr(orch, "_resume_graph", _boom)

    await orch.resume(run_id, {"approved": True})

    async with SessionLocal() as db:
        run = await run_repo.get_by_id(db, run_id)
    assert run is not None
    assert run.status == RunStatus.ERROR.value, (
        f"恢复失败后 run 应收敛成 error，实际 {run.status}"
    )
    assert run.error_msg, "失败原因要留下来"


async def test_resume_failure_emits_terminal_event(monkeypatch):
    """恢复失败必须 emit 终止事件 —— 否则 SSE 连接永远等不到收尾。"""
    run_id = await _make_run()
    orch = Orchestrator()
    hub = get_hub()
    hub.register(run_id, start_seq=0)

    async def _boom(_run_id: int, _decision: dict) -> None:
        """模拟恢复执行时的失败。"""
        raise RuntimeError("模型端点挂了")

    monkeypatch.setattr(orch, "_resume_graph", _boom)

    await orch.resume(run_id, {"approved": True})

    # 事件已落库：找一条 done / error
    from app.repo.run_event_repo import run_event_repo

    async with SessionLocal() as db:
        events = await run_event_repo.list_after(db, run_id, 0)
    kinds = [e.event_type for e in events]
    assert EventType.DONE.value in kinds, f"应发出终止事件，实际 {kinds}"
    assert EventType.ERROR.value in kinds, f"应发出 error 事件，实际 {kinds}"


async def test_resume_cancelled_finishes_the_run(monkeypatch):
    """恢复执行时被取消 → run 收敛成 cancelled，而不是留在 running。

    ``RunCancelled`` 与 ``RunRejected`` 是兄弟类，不捕它就等于漏了一条路径。
    """
    from app.core.errors import RunCancelled

    run_id = await _make_run()
    orch = Orchestrator()

    async def _boom(_run_id: int, _decision: dict) -> None:
        """模拟恢复执行时被取消。"""
        raise RunCancelled("用户取消")

    monkeypatch.setattr(orch, "_resume_graph", _boom)

    await orch.resume(run_id, {"approved": True})

    async with SessionLocal() as db:
        run = await run_repo.get_by_id(db, run_id)
    assert run is not None
    assert run.status == RunStatus.CANCELLED.value, (
        f"取消后 run 应收敛成 cancelled，实际 {run.status}"
    )


# --------------------------------------------------- 并发审批决策


async def test_concurrent_decision_only_one_wins():
    """两个并发决策只能有一个生效。

    条件更新 ``WHERE status='pending'`` 命中 0 行时**必须**被识别出来。以前的
    实现丢弃了 rowcount，两个请求都判定「我成功了」，双双拉起恢复任务——
    同一条 LangGraph thread 被两个任务驱动，审批后的节点跑两遍。
    """
    run_id = await _make_run()
    async with SessionLocal() as db:
        await run_approval_repo.create(db, run_id, "gate", "批？")
        await db.commit()

    async def _decide(approved: bool, comment: str) -> object:
        """模拟一个并发请求：先读 pending，再决策。"""
        async with SessionLocal() as db:
            pending = await run_approval_repo.get_pending(db, run_id)
            if pending is None:
                return None
            # 让两个请求都停在「已读到 pending」这一步
            await asyncio.sleep(0.05)
            return await run_approval_repo.decide(
                db, pending.id, approved, comment, "tester"
            )

    results = await asyncio.gather(
        _decide(True, "A 批准"), _decide(False, "B 驳回")
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"只应有一个决策生效，实际 {len(winners)} 个：{results}"

    # 审计记录与胜出的决策一致
    async with SessionLocal() as db:
        rows = await run_approval_repo.list_by_run(db, run_id)
    assert len(rows) == 1
    assert rows[0].status in ("approved", "rejected")
    assert rows[0].status == winners[0].status


# --------------------------------------------------- last_seq 不能写成 0


async def test_finish_writes_real_last_seq():
    """终态回写用真实 seq，而不是调用方手里的 0。

    进程退出那条路径没有终止事件，调用方传的是 0；写进库里就等于把
    「seq 严格递增」这条全局不变量断掉——合成 done 的 SSE id 会回退到 0，
    续传锚点被重置成 0，下次重连从头重放。
    """
    from app.core.run_hub import RunEventDTO

    run_id = await _make_run(status=RunStatus.RUNNING.value)
    hub = get_hub()
    hub.register(run_id, start_seq=0)
    for i in range(3):
        await hub.emit(run_id, EventType.TOKEN, {"content": f"t{i}"})

    orch = Orchestrator()
    await orch._finish(run_id, RunStatus.ERROR, 0, "服务退出")

    async with SessionLocal() as db:
        run = await run_repo.get_by_id(db, run_id)
    assert run is not None
    assert run.last_seq == 3, f"last_seq 应为真实值 3，实际 {run.last_seq}"
    assert isinstance(RunEventDTO, type)


# --------------------------------------------------- 节点重试历史


async def test_retry_attempts_are_recorded():
    """一个节点重试多次，每次尝试都要留下记录。

    表里仍然是一个节点一行（``uk_run_node``），但每次尝试都往
    ``attempts_json`` 追加一项——否则「第 1 次超时、第 2 次才成功」这件事
    永远查不出来，而它恰恰是复盘时最想看的。
    """
    run_id = await _make_run(status=RunStatus.RUNNING.value)
    async with SessionLocal() as db:
        await run_repo.start_step(db, run_id, "n1", "agent-a", {"input": "x"})
        await db.commit()
        await run_repo.finish_step(db, run_id, "n1", StepStatus.ERROR, None, "超时")

        await run_repo.start_step(db, run_id, "n1", "agent-a", {"input": "x"})
        await db.commit()
        await run_repo.finish_step(db, run_id, "n1", StepStatus.DONE, {"text": "ok"})

        steps = await run_repo.list_steps(db, run_id)

    assert len(steps) == 1, "一个节点仍然只有一行"
    attempts = steps[0].attempts_json or []
    assert len(attempts) == 2, f"两次尝试都该留下，实际 {attempts}"
    assert attempts[0]["status"] == StepStatus.ERROR.value
    assert attempts[0]["error_msg"] == "超时"
    assert attempts[1]["status"] == StepStatus.DONE.value
    assert steps[0].status == StepStatus.DONE.value
