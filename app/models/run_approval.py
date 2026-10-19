"""run_approval 表 —— 人工审批记录（V3.1，设计文档 §16）。

一条 run 走到审批节点时会开一条 pending 记录；人来决策后它变成 approved 或
rejected，并记下是谁、什么时候、说了什么。

**这张表是审计记录，不是「暂停点」本身。** 暂停点（图状态）由 LangGraph
checkpointer 存（见 :mod:`app.core.checkpoint`）。两者分工明确：

- checkpointer 回答「图停在哪、状态是什么」——恢复执行需要它；
- 这张表回答「谁在什么时候批准/驳回了什么」——审计与前端展示需要它。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, PK, Base


class RunApproval(Base):
    """一次审批请求及其决策。"""

    __tablename__ = "run_approval"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # 审批节点在 definition 里的 id
    node_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # pending / approved / rejected
    status: Mapped[str] = mapped_column(sa.String(16), default="pending", nullable=False)
    # 审批节点上写的 prompt，原样存下来：审批单要能独立于 workflow 配置读懂
    prompt: Mapped[str] = mapped_column(sa.Text, default="")
    # 决策人的意见（批准时通常是空，驳回时必填）
    comment: Mapped[str | None] = mapped_column(sa.Text, default=None)
    # 谁做的决策（前端传的 user_id / 姓名）
    decided_by: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    requested_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )
    decided_at: Mapped[object | None] = mapped_column(DATETIME3, default=None)

    __table_args__ = (
        # 一个 run 里同一个审批节点只会遇到一次（DAG 里节点最多跑一次），
        # 所以 (run_id, node_id) 唯一。
        sa.UniqueConstraint("run_id", "node_id", name="uk_run_approval_node"),
        # 支撑「查某次 run 的待审批项」与后台巡检
        sa.Index("idx_approval_status", "status", "requested_at"),
    )

    @property
    def is_pending(self) -> bool:
        """是否还在等人决策。"""
        return self.status == "pending"

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。带上 node_id 与 status，便于定位卡在哪个审批节点。"""
        return f"<RunApproval run={self.run_id} node={self.node_id} {self.status}>"
