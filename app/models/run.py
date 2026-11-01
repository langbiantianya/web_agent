"""run / run_step 表 —— 一次协作任务的生命周期（设计文档 §4.2）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base
from app.models.enums import RunStatus, StepStatus


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    workflow_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), default=RunStatus.PENDING.value, nullable=False
    )
    current_node: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    last_seq: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    error_msg: Mapped[str | None] = mapped_column(sa.Text, default=None)
    started_at: Mapped[object | None] = mapped_column(sa.DateTime, default=None)
    finished_at: Mapped[object | None] = mapped_column(sa.DateTime, default=None)
    created_at: Mapped[object] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        # 支撑 GET /conversations/:id/active-run
        sa.Index("idx_conv_status", "conversation_id", "status"),
        # 支撑启动时扫描遗留 running run
        sa.Index("idx_status", "status", "created_at"),
    )

    @property
    def is_terminal(self) -> bool:
        """run 是否已到终态。

        终态意味着不会再有新事件产生——SSE 流据此补一个合成 ``done`` 后关闭，
        重连时也据此判断是「回放剩余」还是「直接收尾」。
        """
        return RunStatus(self.status).terminal

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。"""
        return f"<Run {self.id} {self.status}>"


class RunStep(Base):
    __tablename__ = "run_step"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    node_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    agent_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), default=StepStatus.PENDING.value, nullable=False
    )
    input_json: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    output_json: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    error_msg: Mapped[str | None] = mapped_column(sa.Text, default=None)
    started_at: Mapped[object | None] = mapped_column(DATETIME3, default=None)
    finished_at: Mapped[object | None] = mapped_column(DATETIME3, default=None)
    # **每次尝试**的明细，JSON 数组，一项一个尝试：
    # ``[{"n":1,"status":"error","error_msg":"...","started_at":"...","finished_at":"..."}]``
    #
    # 为什么单独存一份而不是「一个节点一行、重试就覆盖」：重试是编排器显式控制
    # 的，一次节点失败重试 3 次是常见路径，覆盖掉之后「第 1 次超时、第 2 次才
    # 成功」就再也查不出来了——而这恰恰是复盘时最想看的东西（设计文档 §14.2
    # 「写 run_step 记录每次尝试」）。表里仍然是一个节点一行（``uk_run_node``），
    # 每次尝试作为数组的一项追加进去，于是「重试了几次」与「每次为什么失败」
    # 都能从同一行读出来。
    attempts_json: Mapped[list | None] = mapped_column(JSON_COL, default=None)

    __table_args__ = (
        sa.UniqueConstraint("run_id", "node_id", name="uk_run_node"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。带 node_id 与 status，便于定位卡在流水线哪个节点。"""
        return f"<RunStep run={self.run_id} node={self.node_id} {self.status}>"
