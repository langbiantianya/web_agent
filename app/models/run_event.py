"""run_event 表 —— 续传事件流（设计文档 §4.2 / §5.4）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base


class RunEvent(Base):
    __tablename__ = "run_event"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON_COL, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        # seq 唯一 → 回放幂等、去重依据
        sa.UniqueConstraint("run_id", "seq", name="uk_run_seq"),
        # 支撑按时间窗口清理归档事件
        sa.Index("idx_run_created", "run_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。带上 seq 与 event_type——排查续传问题全靠这两个字段。"""
        return f"<RunEvent run={self.run_id} seq={self.seq} {self.event_type}>"
