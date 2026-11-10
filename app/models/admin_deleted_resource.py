"""已删除资源快照表 —— 为管理端删除操作提供短时撤销入口。

删除是**不可逆**的硬删除：资源行被 ``DELETE`` 掉，引用它的编排随即失效。
这里在删除前把整行快照存下来，并在有限时间内保留一个「撤销」令牌，
让误删的管理员能在几秒内把资源原样恢复，而不是重新手填一遍配置。

快照**只**用于撤销：

- 只保存重建该行所需的业务字段，不含任何请求上下文；
- 密文列（如 ``api_key_cipher``）按原样保存——它本来就是密文，
  落在这张表里与落在原表里的风险等级相同；
- 到期即失效（默认 30 分钟），撤销入口也会拒绝过期快照。

表结构刻意保持窄：只有 ``payload`` 一列 JSON，字段清单由
:mod:`services.admin_web.deletion` 按资源类型维护。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base, utcnow


class AdminDeletedResource(Base):
    """一条待撤销的删除记录。"""

    __tablename__ = "admin_deleted_resource"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # 资源类型：agent / llm_endpoint / mcp_server / workflow。
    resource_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    resource_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # 撤销权限归属：只有发起删除的那个管理员能撤销。
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    actor_username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # 重建该行所需的全部字段。
    payload: Mapped[dict] = mapped_column(JSON_COL, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, nullable=False, default=utcnow
    )
    # 撤销窗口截止时间；过期后撤销入口一律拒绝。
    expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)

    __table_args__ = (
        sa.Index(
            "idx_admin_deleted_resource_created",
            "created_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试摘要不打印 payload（可能含密文列）。"""
        return (
            f"<AdminDeletedResource {self.resource_type}:"
            f"{self.resource_code} expires={self.expires_at}>"
        )
