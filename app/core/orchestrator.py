"""编排器：run 生命周期 + workflow 调度（设计文档 §6.1）。

生命周期：
1. 加载 run / conversation / workflow / agent 配置；
2. 装配上下文（会话历史 + 原始用户输入）；
3. 按 ``linear_order()`` 逐节点执行：node_start → LLM 流式吐字 → node_end；
4. 全部节点结束后 emit done；任何异常 emit error 并终止；
5. **无论成败都 finalize**：冲刷 EventWriter 里的最后一批事件、回写 run.last_seq。

关键点：生成完全跑在 ``asyncio.create_task`` 里，与发起它的 HTTP 请求解耦，
请求返回后、甚至客户端断开后，生成仍在后台继续（设计文档 §3.3）。
"""

from __future__ import annotations

import asyncio
import logging

import sqlalchemy as sa

from app.config.db import SessionLocal
from app.config.settings import settings
from app.core.agent_runner import AgentResult, AgentRunner, _client_factory
from app.core.errors import RunCancelled
from app.core.run_hub import RunHub, get_hub
from app.core.workflow_def import WorkflowDefinition, WorkflowNode
from app.models.enums import (
    EventType,
    MessageRole,
    MessageStatus,
    RunStatus,
    StepStatus,
)
from app.models.message import Message
from app.repo.agent_repo import agent_repo
from app.repo.conversation_repo import conversation_repo
from app.repo.message_repo import message_repo
from app.repo.run_repo import run_repo
from app.repo.workflow_repo import workflow_repo

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, hub: RunHub | None = None) -> None:
        """持有 hub 引用与在飞的后台任务表。

        关键状态：
        - ``hub``：默认取全局单例。必须与 SSE 层、取消接口是同一个实例，否则
          看不到同一份 seq 计数器和取消标志；
        - ``_tasks``：run_id → 后台 Task。存在的唯一理由是 shutdown 能把它们
          收干净；排障时也可用它判断「某个 run 的任务还活着吗」。
        """
        self.hub = hub or get_hub()
        self._tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------- 对外

    def spawn(self, run_id: int) -> asyncio.Task:
        """把 run 扔进后台执行并立即返回——不阻塞 HTTP 响应。"""
        task = asyncio.create_task(self.execute(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task

        def _cleanup(_t: asyncio.Task) -> None:
            """任务结束时的回调：从任务表摘掉自己，防止长跑进程内存里堆满死 Task。

            用 ``pop(..., None)`` 而非 ``del``：run 可能在任务结束前就被 cancel
            接口或 shutdown 清掉了表项。
            """
            self._tasks.pop(run_id, None)

        task.add_done_callback(_cleanup)
        return task

    async def cancel(self, run_id: int) -> bool:
        """请求取消：本实例置标志 + 通知持有该 run 的实例。

        V2 下 run 可能正跑在**别的**实例上，所以不能只看 ``hub.is_active``——
        那样用户在 B 实例点「停止」会得到一个「无事发生」而后台照烧 token。
        改成用跨实例口径的 ``is_active_anywhere``。
        """
        if not await self.hub.is_active_anywhere(run_id):
            return False
        await self.hub.request_cancel(run_id)
        return True

    async def shutdown(self) -> None:
        """进程退出时收尾：取消在飞任务 → 关 LLM 连接 → 冲刷所有 run 事件。

        顺序不能乱：先 cancel 编排任务（execute 的 ``except CancelledError``
        分支会尽力写终止事件），再 ``aclose()`` 释放 httpx 连接池，最后才
        ``hub.shutdown()`` 把没落库的尾部事件写下去。提前关掉 hub 会让 execute
        想 emit 也找不到状态。
        """
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        self._tasks.clear()
        await _client_factory.aclose()
        await self.hub.shutdown()

    # ------------------------------------------------------------- 主流程

    async def execute(self, run_id: int) -> None:
        """执行整个 run：装配上下文 → 逐节点跑 → 发 done → 收尾。

        这是全流程唯一需要小心的函数，三条约束贯穿其中：

        1. **无论成败都必须收尾**：成功、取消、异常、进程退出四条路径都要走到
           ``_finish``，把 EventWriter 里没落库的事件冲掉并回写 last_seq。漏掉
           任何一条，续传时就会一直等一个永远不会来的事件。
        2. **emit done 在状态回写之前**：编排器是先 emit 终止事件、再回写 run
           状态。这产生一个「事件已结束、状态仍是 running」的窗口，sse_writer 的
           ⑤ 分支正是为它准备的——先看本次流有没有见过终止事件，再看状态。
        3. **配置缺失要在花钱前失败**：Agent 缺失/禁用、用户消息缺失都在
           ``_load_plan`` 阶段抛，绝不跑到一半才失败。

        Args:
            run_id: 要执行的 run。真实执行体由 ``spawn`` 放进后台 task，与发起
                它的 HTTP 请求解耦——客户端断开也不影响生成。
        """
        run_snapshot: dict | None = None
        try:
            plan = await self._load_plan(run_id)
            if plan is None:
                logger.error("run %s 或其依赖配置不存在，直接结束", run_id)
                return

            run_snapshot = plan
            definition = plan["definition"]
            agents = plan["agents"]
            user_input = plan["user_input"]

            # seq 计数器从上次中断处续接（正常情况都是 0）
            self.hub.register(run_id, start_seq=plan["start_seq"])
            # 先抢归属租约再开跑：租约是「别人不会误杀这个 run」的唯一凭据，
            # 必须早于任何生成动作建立，否则别的实例在它之前启动过一次
            # recover_stale_runs 就会把这条 run 标成 error。
            await self.hub.claim(run_id)
            await run_repo_mark_running(run_id)

            async with SessionLocal() as db:
                context = await message_repo.load_context(
                    db, plan["conversation_id"], exclude_pending=False
                )
            # 末尾那条就是本次刚写入的用户消息，单独取出用于渲染模板
            if context and context[-1]["role"] == MessageRole.USER.value:
                context.pop()

            upstream: dict[str, str] = {}
            usage_total = {"prompt_tokens": 0, "completion_tokens": 0}

            for node in definition.linear_order():
                if self.hub.is_cancelled(run_id):
                    raise RunCancelled("用户取消")

                agent = agents[node.agentCode]
                await self.hub.emit(
                    run_id,
                    EventType.NODE_START,
                    {"node": node.id, "agentCode": node.agentCode, "model": agent.model},
                )
                await run_repo_set_node(run_id, node.id)

                rendered = definition.render_input(user_input, upstream)
                result = await self._run_node_with_retry(
                    run_id=run_id,
                    node=node,
                    agent=agent,
                    context=context,
                    rendered_input=rendered,
                )

                usage_total["prompt_tokens"] += result.usage.get("prompt_tokens", 0)
                usage_total["completion_tokens"] += result.usage.get("completion_tokens", 0)

                output_key = node.outputKey or node.id
                upstream[output_key] = result.text

                await self.hub.emit(
                    run_id,
                    EventType.NODE_END,
                    {"node": node.id, "outputKey": output_key, "agentCode": node.agentCode},
                )

                # 节点产出落库，成为后续节点的上下文
                await self._persist_assistant_message(
                    run_id, plan["conversation_id"], node.agentCode, result.text
                )
                context = context + [{"role": "assistant", "content": result.text}]

            last_seq = (
                await self.hub.emit(
                    run_id,
                    EventType.DONE,
                    {"status": RunStatus.DONE.value, "usage": usage_total},
                )
            ).seq
            await self._finish(run_id, RunStatus.DONE, last_seq)

        except RunCancelled as exc:
            last_seq = (
                await self.hub.emit(
                    run_id,
                    EventType.DONE,
                    {"status": RunStatus.CANCELLED.value, "message": str(exc)},
                )
            ).seq
            await self._finish(run_id, RunStatus.CANCELLED, last_seq, "用户取消")
            logger.info("run %s 已取消", run_id)

        except asyncio.CancelledError:
            # 进程退出：尽力收尾后向上抛
            await self._finish(run_id, RunStatus.ERROR, 0, "服务退出")
            raise

        except Exception as exc:  # noqa: BLE001
            logger.exception("run %s 执行失败", run_id)
            try:
                await self.hub.emit(
                    run_id,
                    EventType.ERROR,
                    {"message": str(exc), "type": type(exc).__name__},
                )
                last_seq = (
                    await self.hub.emit(
                        run_id, EventType.DONE, {"status": RunStatus.ERROR.value}
                    )
                ).seq
                await self._finish(run_id, RunStatus.ERROR, last_seq, str(exc)[:2000])
            except Exception:  # pragma: no cover
                logger.exception("run %s 失败后收尾也失败了", run_id)

    # ------------------------------------------------------------- 节点

    async def _run_node_with_retry(
        self,
        run_id: int,
        node: WorkflowNode,
        agent,
        context: list[dict],
        rendered_input: str,
    ) -> AgentResult:
        """指数退避重试（设计文档 §3.3：失败重试 2 次）。"""
        attempts = settings.node_max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            if self.hub.is_cancelled(run_id):
                raise RunCancelled("用户取消")
            try:
                messages = (
                    [{"role": MessageRole.SYSTEM.value, "content": agent.system_prompt}]
                    + context
                    + [{"role": MessageRole.USER.value, "content": rendered_input}]
                )
                await self._start_step(run_id, node, agent, {"input": rendered_input})

                runner = AgentRunner(
                    agent=agent,
                    node_id=node.id,
                    run_id=run_id,
                    hub=self.hub,
                    is_cancelled=lambda: self.hub.is_cancelled(run_id),
                )
                result = await runner.run(messages)
                await self._finish_step(
                    run_id,
                    node.id,
                    StepStatus.DONE,
                    {"text": result.text, "toolCalls": result.tool_calls},
                )
                return result

            except RunCancelled:
                await self._finish_step(run_id, node.id, StepStatus.ERROR, None, "已取消")
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "run %s 节点 %s 第 %s/%s 次失败：%s",
                    run_id, node.id, attempt, attempts, exc,
                )
                await self._finish_step(run_id, node.id, StepStatus.ERROR, None, str(exc))
                if attempt >= attempts:
                    break
                await asyncio.sleep(settings.node_retry_base_delay * (2 ** (attempt - 1)))
                if self.hub.is_cancelled(run_id):
                    raise RunCancelled("用户取消") from exc

        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------- 辅助

    async def _load_plan(self, run_id: int) -> dict | None:
        """一次性把 run 需要的全部配置读出来，避免长事务占着连接。"""
        async with SessionLocal() as db:
            run = await run_repo.get_by_id(db, run_id)
            if run is None:
                return None
            conv = await conversation_repo.get_by_id(db, run.conversation_id)
            wf = await workflow_repo.get_by_id(db, run.workflow_id)
            if conv is None or wf is None:
                return None

            definition = WorkflowDefinition.model_validate(wf.definition)
            codes = [n.agentCode for n in definition.nodes]
            agents = await agent_repo.get_by_codes(db, codes)

            missing = [c for c in codes if c not in agents or not agents[c].enabled]
            if missing:
                raise ValueError(f"Agent 未配置或已禁用：{missing}")

            # 用户输入取本 run 对应的最后一条 user message
            user_input = await db.scalar(
                sa.select(Message.content)
                .where(
                    Message.run_id == run_id,
                    Message.role == MessageRole.USER.value,
                )
                .order_by(Message.id.desc())
                .limit(1)
            )
            if not user_input:
                raise ValueError(f"run {run_id} 没有对应的用户消息")

            return {
                "run_id": run_id,
                "conversation_id": run.conversation_id,
                "workflow_id": run.workflow_id,
                "start_seq": run.last_seq or 0,
                "definition": definition,
                "agents": agents,
                "user_input": user_input,
            }

    async def _persist_assistant_message(
        self, run_id: int, conversation_id: int, agent_code: str, text: str
    ) -> None:
        """把一个节点的产出落成 assistant 消息，供后续节点当上下文读。

        只在**节点结束时**写一次，而不是边流边写：运行中的 message 若落库，重启
        恢复时无法区分「写到一半」和「真的就这么短」。已完成的消息不可变，续传
        拿不到流时前端可以直接用它兜底。

        每个节点一条消息，因此 agent_code 必须带上——同一个会话里多个节点的
        产出要能区分来源。
        """
        async with SessionLocal() as db:
            await message_repo.add(
                db,
                conversation_id=conversation_id,
                run_id=run_id,
                agent_code=agent_code,
                role=MessageRole.ASSISTANT.value,
                content=text,
                status=MessageStatus.DONE.value,
            )
            await db.commit()

    async def _start_step(self, run_id: int, node: WorkflowNode, agent, input_json: dict):
        """记录一次节点尝试的开始（run_step 落一行）。

        每次**尝试**都新起一条 step 而不复用上一条：重试是编排器显式控制的，
        一条 step 只描述一次尝试，事后才能看清「第 1 次超时、第 2 次才成功」。
        """
        async with SessionLocal() as db:
            await run_repo.start_step(db, run_id, node.id, agent.code, input_json)
            await db.commit()

    async def _finish_step(
        self,
        run_id: int,
        node_id: str,
        status: StepStatus,
        output_json: dict | None = None,
        error_msg: str | None = None,
    ) -> None:
        """给一次节点尝试收尾：写终态、产出或错误信息。

        成功与失败都要走这里（失败传 ``error_msg``），因为 step 表是对「这次尝试
        到底发生了什么」的记录——只在成功时写的话，重试历史就永远查不出来。

        Args:
            status: done / error，用 StepStatus 区分「节点失败」与「用户取消」。
            output_json: 节点产出与工具调用；失败时为 None，不留半截数据。
            error_msg: 失败原因，取消场景下用于区分「主动取消」与「真出错」。

        这里刻意不 commit——``run_repo.finish_step`` 自行管理事务边界。
        """
        async with SessionLocal() as db:
            await run_repo.finish_step(db, run_id, node_id, status, output_json, error_msg)

    async def _finish(
        self, run_id: int, status: RunStatus, last_seq: int, error_msg: str | None = None
    ) -> None:
        """冲刷事件 → 回写 run 状态与 last_seq（避免写放大）。"""
        try:
            await self.hub.finalize(run_id)
        except Exception:  # pragma: no cover
            logger.exception("run %s finalize 失败", run_id)
        async with SessionLocal() as db:
            await run_repo.finish(db, run_id, status, last_seq, error_msg)


# ---- 模块级小函数：每次开一个短连接，避免跨整个 run 持有事务 ----


async def run_repo_mark_running(run_id: int) -> None:
    """把 run 置为 running（每次尝试开始时调用）。

    用独立短事务而不是并进 ``_load_plan`` 那个 Session：run 的生成可能持续几分钟，
    把连接攥在手里不放会迅速耗尽连接池。V2 断点续跑时这里也是唯一的「重新标记」
    入口，所以执行前必须已读到 ``run.last_seq`` 并注册过 hub 状态。
    """
    async with SessionLocal() as db:
        await run_repo.mark_running(db, run_id)


async def run_repo_set_node(run_id: int, node_id: str) -> None:
    """记录「当前正在跑哪个节点」。

    在 emit node_start 之后调用，所以这个字段回答的是「卡在哪一步」——用户看到
    一个不再吐字的 run 时，靠它判断是 LLM 慢还是已经挂了。同样是短事务：只是
    一个进度标记，不值得为它占着连接。
    """
    async with SessionLocal() as db:
        await run_repo.set_current_node(db, run_id, node_id)


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    """全局单例编排器。

    与 ``get_hub()`` 同理：编排器持有「在飞 run 的后台任务表」，必须全进程一份，
    否则 shutdown 收不干净、某些 run 会在应用退出时变成永久的 running。

    同为无 await 的惰性构造，单事件循环下不存在并发创建问题。
    """
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


async def recover_stale_runs() -> int:
    """进程启动时把**无主**的 running/pending run 标记为 error（设计文档 §9）。

    V1 的做法是「扫到 running 就标 error」，单实例下正确。多实例下这是**硬伤**：
    B 实例每次启动都会把 A 实例正在生成的 run 全部标成 error，用户眼睁睁看着
    自己的回答在生成中途被判死刑——多活反而比单活更糟。

    V2 用归属租约区分「真的没人管了」和「在别的实例上跑得好好的」：

    - 租约键还在 → 有人管着，**跳过**；
    - 租约键不存在或已过期 → 生成方已死（进程被 kill 之后没有人续期），
      标记成 error 是正确且必要的，否则这些 run 会永远停在 running。

    租约天然带 TTL，所以不需要额外的「心跳表」或「实例注册表」——实例被
    kill -9 时不会有任何清理动作发生，键自己过期就够了。
    """
    from app.core.lease import get_lease

    lease = get_lease()
    async with SessionLocal() as db:
        stale = await run_repo.find_stale_running(db)
        count = 0
        skipped = 0
        for run in stale:
            if await lease.is_owned(run.id):
                logger.info("run %s 仍由其它实例生成，跳过恢复", run.id)
                skipped += 1
                continue
            await run_repo.finish(
                db,
                run.id,
                RunStatus.ERROR,
                run.last_seq or 0,
                "服务重启导致生成中断，请重新发送",
            )
            count += 1
        if count:
            logger.warning("已将 %s 个遗留 run 标记为 error", count)
        if skipped:
            logger.info("跳过 %s 个仍被其它实例持有的 run", skipped)
        return count


__all__ = [
    "Orchestrator",
    "get_orchestrator",
    "recover_stale_runs",
]
