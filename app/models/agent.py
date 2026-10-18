"""agent 表 —— Agent 配置（设计文档 §4.2）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSON_COL, PK, Base


class Agent(Base):
    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    description: Mapped[str] = mapped_column(sa.String(512), default="")
    system_prompt: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    api_key_cipher: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    temperature: Mapped[float] = mapped_column(sa.DECIMAL(3, 2), default=0.70)
    max_tokens: Mapped[int] = mapped_column(sa.Integer, default=2048)
    tools: Mapped[list | None] = mapped_column(JSON_COL, default=None)
    extra_params: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    enabled: Mapped[bool] = mapped_column(sa.SmallInteger, default=1)
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
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
        sa.Index("idx_agent_enabled", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。刻意只显示 code 与 version，不含 api_key_cipher。"""
        return f"<Agent {self.code} v{self.version}>"
