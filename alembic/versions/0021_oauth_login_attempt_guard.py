"""oauth_login_attempt 加限频与故障观测列（V4.10 加固）。

Revision ID: b3f7c1d95ae2
Revises: c7d2e5a8b134
Create Date: 2026-11-29

登录限频与 provider 故障监控都要落到这张表上，所以给它加三列：

- ``client_ip_hash``：客户端 IP 的哈希（掺了 SECRET_KEY），限频按它分组，
  但**不存明文 IP**；
- ``failure_code``：这次登录事务最终失败的稳定错误码；
- ``failed_at``：失败发生的时间，失败限频与失败率统计都按它取窗口。

外加一个 ``(provider_id, client_ip_hash, created_at)`` 复合索引——限频查询
的主路径就是按这个组合取窗口内的行。

存量行的三列都是 NULL：它们既不属于任何 IP 分组，也没有失败码，因此既不参与
限频计数，也不参与失败率统计——升级后**不会**凭空多出一次失败。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "b3f7c1d95ae2"
down_revision = "c7d2e5a8b134"
branch_labels = None
depends_on = None

# 与 :mod:`app.models.base` 的 DATETIME3 保持一致：MySQL 用 DATETIME(3)，
# 其它方言回落成通用 DateTime。
DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")


def upgrade() -> None:
    """给 oauth_login_attempt 加三列与一个复合索引。"""
    with op.batch_alter_table("oauth_login_attempt") as batch:
        batch.add_column(sa.Column("client_ip_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("failure_code", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("failed_at", DATETIME3, nullable=True))
        batch.create_index(
            "idx_oauth_attempt_provider_ip",
            ["provider_id", "client_ip_hash", "created_at"],
        )


def downgrade() -> None:
    """移除三列与索引。

    降级后限频与 provider 故障监控会失去数据来源（代码里对应功能需要一并关闭），
    但**已经产生的登录会话不受影响**——它们在 user_session 表里，与本表无关。
    """
    with op.batch_alter_table("oauth_login_attempt") as batch:
        batch.drop_index("idx_oauth_attempt_provider_ip")
        batch.drop_column("failed_at")
        batch.drop_column("failure_code")
        batch.drop_column("client_ip_hash")