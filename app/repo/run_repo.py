"""Run / RunStep 数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.enums import RunStatus, StepStatus
from app.models.run import Run, RunStep

# upsert 允许写入的列。``run_id`` / ``node_id`` 是冲突键本身，不能出现在
# SET 子句里——方言对「把冲突键改成新值」有的直接报错，有的会写出无意义的
# 自赋值。
_STEP_UPSERT_COLUMNS = frozenset(
    {
        "run_id",
        "node_id",
        "agent_code",
        "status",
        "input_json",
        "output_json",
        "error_msg",
        "started_at",
        "finished_at",
        "attempts_json",
    }
)


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
        """该会话是否已有 pending/running/waiting 的 run（用于 409 判定）。

        ``waiting``（V3.1 等待人工审批）也算「进行中」：审批没结束之前，这个
        会话不该再发新消息——否则同一个会话会有两条并行的 run，消息上下文会串。
        """
        stmt = sa.select(Run).where(
            Run.conversation_id == conversation_id,
            Run.status.in_([
                RunStatus.PENDING.value,
                RunStatus.RUNNING.value,
                RunStatus.WAITING.value,
            ]),
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

    async def pause(
        self, db: AsyncSession, run_id: int, last_seq: int, node_id: str
    ) -> None:
        """把 run 置为 waiting（V3.1：暂停在审批节点上等人决策）。

        **不是终态**：run 还会被恢复继续跑，所以 ``finished_at`` 保持为空、
        ``error_msg`` 不写。``last_seq`` 必须写准——恢复执行时新实例要靠它把
        seq 计数器接上，写错了会与 ``run_event`` 的 ``uk_run_seq`` 撞车。
        """
        await db.execute(
            sa.update(Run)
            .where(Run.id == run_id)
            .values(
                status=RunStatus.WAITING.value,
                last_seq=last_seq,
                current_node=node_id,
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
        """按 (run_id, node_id) 取节点执行记录；uk_run_node 保证唯一。

        带 ``populate_existing``：这一步的返回值会被调用方**改写后回写**
        （追加一次尝试的记录），所以必须拿到库里最新的值。会话里已有的同名
        实例默认不会被 SELECT 刷新——不加这个选项，第二次尝试读到的可能是
        第一次尝试留下的旧 ``attempts_json``，历史就被悄悄截断了。
        """
        stmt = (
            sa.select(RunStep)
            .where(RunStep.run_id == run_id, RunStep.node_id == node_id)
            .execution_options(populate_existing=True)
        )
        return await db.scalar(stmt)

    async def upsert_step(self, db: AsyncSession, **kwargs) -> RunStep:
        """创建或更新节点执行记录。

        **原子 upsert**：走方言原生的 upsert 语句——MySQL 用
        ``ON DUPLICATE KEY UPDATE``、PostgreSQL 与 SQLite 用
        ``ON CONFLICT ... DO UPDATE``。

        曾经这里是「先查后写」（SELECT 一次，没有再 INSERT）：并发下两个执行
        会同时查到「不存在」，双双 INSERT，后者撞 ``uk_run_node`` 抛
        ``IntegrityError``。而那个异常会被编排器当成「节点失败」白白重试一轮
        ——真正的错误信息反而被埋掉了。方言原生 upsert 没有这个窗口。

        Args:
            db: 会话。
            **kwargs: run_step 字段，至少含 run_id 与 node_id。
        """
        run_id = kwargs["run_id"]
        node_id = kwargs["node_id"]
        dialect = db.bind.dialect.name

        # 只把「这次要写的列」挑出来——upsert 的 SET 子句不该把没传的列清空。
        values = {k: v for k, v in kwargs.items() if k in _STEP_UPSERT_COLUMNS}

        if dialect == "mysql":
            stmt = mysql_insert(RunStep).values(**values)
            stmt = stmt.on_duplicate_key_update(
                **{k: v for k, v in values.items() if k not in ("run_id", "node_id")}
            )
        else:
            # SQLite 与 PostgreSQL 的语法一致，只是插入器来自各自的方言
            insert = sqlite_insert if dialect == "sqlite" else pg_insert
            stmt = insert(RunStep).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["run_id", "node_id"],
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in ("run_id", "node_id")
                },
            )

        await db.execute(stmt)
        await db.flush()
        step = await self.get_step(db, run_id, node_id)
        if step is None:  # pragma: no cover - upsert 成功后必然读得到
            raise RuntimeError(f"run_step upsert 后读不到：run={run_id} node={node_id}")
        return step

    async def start_step(
        self, db: AsyncSession, run_id: int, node_id: str, agent_code: str, input_json: dict
    ) -> RunStep:
        """标记节点开始执行并记录输入。

        一个节点**每次尝试**都往 ``attempts_json`` 里追加一项：重试时旧的那次
        不会被覆盖，于是「第 1 次超时、第 2 次才成功」在复盘时看得见。
        ``status`` / ``error_msg`` 这两个字段仍然表示**最近一次尝试**的状态，
        让「这个节点现在怎么样了」这种查询不用解 JSON。
        """
        step = await self.upsert_step(
            db,
            run_id=run_id,
            node_id=node_id,
            agent_code=agent_code,
            status=StepStatus.RUNNING.value,
            input_json=input_json,
            started_at=utcnow(),
        )
        attempts = list(step.attempts_json or [])
        attempts.append(
            {
                "n": len(attempts) + 1,
                "status": StepStatus.RUNNING.value,
                "error_msg": None,
                "started_at": utcnow().isoformat(),
                "finished_at": None,
            }
        )
        step.attempts_json = attempts
        # 新一次尝试开始时把上一次残留的 error_msg 清掉——否则行上会同时出现
        # 「status=running」和「error_msg=上一次的错误」，看着像自相矛盾。
        step.error_msg = None
        await db.flush()
        return step

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

        同时把这次尝试的终态回填到 ``attempts_json`` 的最后一项——那是这一行里
        **唯一**能区分「第 1 次失败」和「第 2 次成功」的地方。
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
        step = await self.get_step(db, run_id, node_id)
        if step is not None:
            attempts = list(step.attempts_json or [])
            if attempts:
                last = dict(attempts[-1])
                last["status"] = status.value
                last["error_msg"] = error_msg
                last["finished_at"] = utcnow().isoformat()
                attempts[-1] = last
                step.attempts_json = attempts
        await db.commit()

    async def list_steps(self, db: AsyncSession, run_id: int) -> list[RunStep]:
        """列出某 run 的全部节点执行记录，按 id 正序即实际执行顺序。"""
        stmt = (
            sa.select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.id.asc())
        )
        return list(await db.scalars(stmt))


run_repo = RunRepo()
