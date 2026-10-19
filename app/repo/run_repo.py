"""Run / RunStep 数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.enums import RunStatus, StepStatus
from app.models.run import Run, RunStep


class RunRepo:
    # ------------------------------------------------------------------ run

    async def create(self, db: AsyncSession, **kwargs) -> Run:
        """新建 run，只 flush 不 commit：事件流尚未产出，事务边界交给编排层。

        Args:
            db: 依赖注入的请求级 AsyncSession。
            **kwargs: run 表字段，至少含 conversation_id 与 workflow_id。
        """
        run = Run(**kwargs)
        db.add(run)
        await db.flush()
        return run

    async def get_by_id(self, db: AsyncSession, run_id: int) -> Run | None:
        """按自增 id 取 run。"""
        return await db.get(Run, run_id)

    async def get_active_by_conversation(
        self, db: AsyncSession, conversation_id: int
    ) -> Run | None:
        """该会话是否已有 pending/running 的 run（用于 409 判定）。"""
        stmt = sa.select(Run).where(
            Run.conversation_id == conversation_id,
            Run.status.in_([RunStatus.PENDING.value, RunStatus.RUNNING.value]),
        )
        return await db.scalar(stmt.limit(1))

    async def mark_running(self, db: AsyncSession, run_id: int) -> None:
        """把 run 从 pending 推进到 running 并记 started_at（一个 run 只应走到这一步一次）。

        时间戳一律走 :func:`~app.models.base.utcnow`（UTC 裸值）：同一张表的
        ``created_at`` 是数据库 ``now()`` 写的 UTC，用本地时间写 ``started_at``
        会让同一行里混进两个相差 8 小时的时间戳。
        """
        await db.execute(
            sa.update(Run)
            .where(Run.id == run_id)
            .values(status=RunStatus.RUNNING.value, started_at=utcnow())
        )
        await db.commit()

    async def set_current_node(self, db: AsyncSession, run_id: int, node_id: str) -> None:
        """记录当前执行到的节点，供进度查询与进程崩溃后的人工排查。"""
        await db.execute(sa.update(Run).where(Run.id == run_id).values(current_node=node_id))
        await db.commit()

    async def finish(
        self,
        db: AsyncSession,
        run_id: int,
        status: RunStatus,
        last_seq: int,
        error_msg: str | None = None,
    ) -> None:
        """结束 run 并统一回写 last_seq（避免每 token UPDATE 造成写放大）。"""
        await db.execute(
            sa.update(Run)
            .where(Run.id == run_id)
            .values(
                status=status.value,
                last_seq=last_seq,
                error_msg=error_msg,
                finished_at=utcnow(),
            )
        )
        await db.commit()

    async def find_stale_running(self, db: AsyncSession) -> list[Run]:
        """启动时扫描上次进程遗留的 running run（设计文档 §9）。"""
        stmt = sa.select(Run).where(
            Run.status.in_([RunStatus.RUNNING.value, RunStatus.PENDING.value])
        )
        return list(await db.scalars(stmt))

    async def list_by_conversation(
        self, db: AsyncSession, conversation_id: int, limit: int = 50
    ) -> list[Run]:
        """列出会话下的历史 run，按 id 倒序（最近一次在前）。"""
        stmt = (
            sa.select(Run)
            .where(Run.conversation_id == conversation_id)
            .order_by(Run.id.desc())
            .limit(limit)
        )
        return list(await db.scalars(stmt))

    # -------------------------------------------------------------- run_step

    async def get_step(self, db: AsyncSession, run_id: int, node_id: str) -> RunStep | None:
        """按 (run_id, node_id) 取节点执行记录；uk_run_node 保证唯一。"""
        stmt = sa.select(RunStep).where(RunStep.run_id == run_id, RunStep.node_id == node_id)
        return await db.scalar(stmt)

    async def upsert_step(self, db: AsyncSession, **kwargs) -> RunStep:
        """创建或更新节点执行记录（重试时复用同一条记录）。"""
        step = await self.get_step(db, kwargs["run_id"], kwargs["node_id"])
        if step is None:
            step = RunStep(**kwargs)
            db.add(step)
        else:
            for k, v in kwargs.items():
                setattr(step, k, v)
        await db.flush()
        return step

    async def start_step(
        self, db: AsyncSession, run_id: int, node_id: str, agent_code: str, input_json: dict
    ) -> RunStep:
        """标记节点开始执行并记录输入。

        走 upsert 而非纯 insert：节点重试时复用同一条记录（uk_run_node），
        这样一次 run 内每个节点始终只有一行执行历史。
        """
        return await self.upsert_step(
            db,
            run_id=run_id,
            node_id=node_id,
            agent_code=agent_code,
            status=StepStatus.RUNNING.value,
            input_json=input_json,
            started_at=utcnow(),
        )

    async def finish_step(
        self,
        db: AsyncSession,
        run_id: int,
        node_id: str,
        status: StepStatus,
        output_json: dict | None = None,
        error_msg: str | None = None,
    ) -> None:
        """收尾节点执行：写终态、输出与错误信息，并记 finished_at。

        用 UPDATE 直写而非加载实体，避开 ORM 身份冲突，也省掉一次无用回读；
        注意这里会 commit，与只 flush 的 upsert/start_step 不同。
        """
        await db.execute(
            sa.update(RunStep)
            .where(RunStep.run_id == run_id, RunStep.node_id == node_id)
            .values(
                status=status.value,
                output_json=output_json,
                error_msg=error_msg,
                finished_at=utcnow(),
            )
        )
        await db.commit()

    async def list_steps(self, db: AsyncSession, run_id: int) -> list[RunStep]:
        """列出某 run 的全部节点执行记录，按 id 正序即实际执行顺序。"""
        stmt = (
            sa.select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id.asc())
        )
        return list(await db.scalars(stmt))


run_repo = RunRepo()
