"""workflow 表加 version 列

Revision ID: 7c1f0a94b2e6
Revises: cf7910dfd924
Create Date: 2026-10-19

给 workflow 加 ``version``，让缓存能像 agent 那样靠版本号自然失效，不再依赖
调用方记得 ``put()`` / ``clear()``。

回填策略用 ``server_default`` 而不是 UPDATE：加列当步就能把所有既有行填成 1，
省掉一次全表 UPDATE。SQLite 的 ADD COLUMN 不支持非常量 DEFAULT，
``sa.func.now()`` 这类表达式在这里用不了，所以走普通常量 + 显式回填两步。

降级只回填值、不删列：删列在 SQLite 上要重建表，会把 definition 这个 JSON
列也一起搬一遍，风险收益不划算；而留着 version 列对旧代码无害（ORM 有默认值）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "7c1f0a94b2e6"
down_revision = "cf7910dfd924"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """加 version 列并把所有既有行回填成 1。

    **先查列是否已存在**：历史版本的 downgrade 是空操作（只把 alembic_version
    标记退回去，列还留在表里）。从那种状态再 upgrade 到本版时，直接 ADD COLUMN
    会撞「duplicate column name」。这里先看一眼，避免把用户卡在一个「回滚过就
    再也升不回来」的版本上。
    """
    if _has_column("workflow", "version"):
        return
    with op.batch_alter_table("workflow") as batch:
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
    # server_default 已经把新行填成 1；显式 UPDATE 一次是为了让
    # 依赖 server_default 的方言（MySQL 允许省略）也拿到确定的值，
    # 同时把 DEFAULT 从「永远存在」降级为「仅回填用」。
    op.execute(sa.text("UPDATE workflow SET version = 1 WHERE version IS NULL"))
    with op.batch_alter_table("workflow") as batch:
        batch.alter_column("version", server_default=None)


def downgrade() -> None:
    """删掉 version 列。

    早先这里是空操作（理由是「删列在 SQLite 上要重建表」），但那会让
    ``alembic_version`` 与实际表结构对不上：回退到 0001 之后再 upgrade，
    0002 会去 ADD 一个已经存在的列，直接报「duplicate column name: version」
    ——三方言皆然。降级必须让结构与版本标记**重新对齐**，否则这条路径就是死的。

    SQLite 上删列走 batch 重建表；definition 是 JSON 列，batch 会原样搬过去。
    """
    if not _has_column("workflow", "version"):
        return
    with op.batch_alter_table("workflow") as batch:
        batch.drop_column("version")


def _has_column(table: str, column: str) -> bool:
    """列是否已存在。

    用 ``inspect`` 而不是 ``has_table``：升级路径要判断的是「列在不在」，
    而离线模式（``--sql``）下没有真实连接可查——离线渲染时一律当作「不存在」，
    按首次升级生成 DDL。
    """
    try:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        if not inspector.has_table(table):
            return False
        return column in {c["name"] for c in inspector.get_columns(table)}
    except Exception:  # pragma: no cover - 离线模式等拿不到真实 schema 的场景
        return False

