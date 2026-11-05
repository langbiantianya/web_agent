"""工具调用审计账本（V4.3）。

``run_event`` 记录对外发生的事件，``run_step.output_json`` 记录节点结果；
本表单独回答「模型实际调用了哪个工具、参数和结果是什么、耗时多少」。工具参数
和结果可能包含业务数据与凭据，因此所有文本在写入前都要脱敏，并受保留期限制。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base

# 审计结果上限默认是 64K 字符。MySQL 的普通 TEXT 只有 64KB，UTF-8 中文会提前
# 达到上限，所以这里显式使用 LONGTEXT；其它方言仍用通用 Text。
AUDIT_TEXT = sa.Text().with_variant(mysql_dialect.LONGTEXT(), "mysql")


class ToolCallAudit(Base):
    """一次工具执行的脱敏审计记录。

    没有外键：审计需要独立于 run、工具配置和会话事务落库。即使后续删除业务
    配置，已经发生的工具调用事实仍应保留到保留期结束。
    """

    __tablename__ = "tool_call_audit"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="default"
    )
    run_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    node_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # 模型提供的 id 可能重复或为空，因此不建唯一约束；每次实际执行都是一条新事实。
    tool_call_id: Mapped[str] = mapped_column(
        sa.String(128), nullable=False, default=""
    )
    tool_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # builtin / skill / mcp。skill 是内置执行入口，但来源单独标出便于运营筛选。
    source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    server: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    arguments_json: Mapped[dict | list | None] = mapped_column(JSON_COL, default=None)
    arguments_redacted: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0
    )
    arguments_chars: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    # 敏感输出被拦截时这里必须为空，不能保存命中正文。
    result_text: Mapped[str | None] = mapped_column(AUDIT_TEXT, nullable=True)
    result_chars: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    result_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    result_truncated: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0
    )
    ok: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=0)
    failure_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )
    expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)

    __table_args__ = (
        sa.Index("idx_tool_audit_tenant_created", "tenant_id", "created_at"),
        sa.Index("idx_tool_audit_run_created", "run_id", "created_at"),
        sa.Index("idx_tool_audit_expires", "expires_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试摘要不输出参数或结果正文。"""
        return (
            f"<ToolCallAudit id={self.id} run={self.run_id} "
            f"tool={self.tool_name} ok={self.ok}>"
        )
