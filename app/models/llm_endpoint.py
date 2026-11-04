"""LLM 接口配置表。

地址与 API Key 属于部署级凭据，不应写进代码或镜像。运行期按 ``priority``
从小到大尝试启用项；同一个 Key 可以配置在多个地址上，也可以用多个 Key
指向同一个兼容接口作为故障切换。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, PK, Base


class LlmEndpoint(Base):
    """一个 OpenAI 兼容接口配置。"""

    __tablename__ = "llm_endpoint"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    api_key_cipher: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    # 数值越小越先尝试；相同 priority 时按 id 保证顺序稳定。
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=100)
    enabled: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("idx_llm_endpoint_enabled_priority", "enabled", "priority"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试摘要不包含密文或地址中的凭据。"""
        return f"<LlmEndpoint {self.code} priority={self.priority}>"
