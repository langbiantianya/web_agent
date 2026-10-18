"""workflow 表 —— 编排定义（设计文档 §4.2）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSON_COL, PK, Base


class Workflow(Base):
    __tablename__ = "workflow"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    description: Mapped[str] = mapped_column(sa.String(512), default="")
    definition: Mapped[dict] = mapped_column(JSON_COL, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.SmallInteger, default=1)
    created_at: Mapped[object] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        sa.DateTime,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("idx_workflow_enabled", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。"""
        return f"<Workflow {self.code}>"
