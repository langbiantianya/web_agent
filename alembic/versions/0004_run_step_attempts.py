"""run_step 加 attempts_json 列 + 修 run_approval 的时间列精度

Revision ID: f1a2b3c4d5e6
Revises: c4d8e1f0a927
Create Date: 2026-11-01

两处改动：

1. ``run_step`` 加 ``attempts_json`` JSON 列——**每次尝试**的明细，一项一个尝试。
   表里仍然是一个节点一行（``uk_run_node`` 不变），每次尝试作为数组的一项追加
   进去，于是「第 1 次超时、第 2 次才成功」在复盘时看得见，而不会像以前那样
   被下一次尝试直接覆盖掉。

2. 把 ``run_approval`` 的两个时间列修成 ``DATETIME(3)``。0003 建表时用的是裸
   ``sa.DateTime()``，在 MySQL 上落成 ``DATETIME``（fsp=0），与 ORM 期望的
   ``DATETIME(3)`` 不一致：审批时间被截到整秒，且 ``env.py`` 开了
   ``compare_type=True``，此后每次 autogenerate 都会持续产出多余的 ALTER 差异。
   PostgreSQL 与 SQLite 上两种写法渲染结果相同（TIMESTAMP / DATETIME），不需要
   改，所以这里只对 MySQL 下手。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "f1a2b3c4d5e6"
down_revision = "c4d8e1f0a927"
branch_labels = None
depends_on = None

# 与 app.models.base.DATETIME3 保持一致：MySQL 用 fsp=3，其余方言用通用 DateTime
DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")

# MySQL 的**表达式默认值必须带括号**：``DEFAULT (now())`` 合法，``DEFAULT now()``
# 会被服务端拒绝，报 (1067, "Invalid default value for 'requested_at'")。
#
# 这个差异只在 ALTER 路径上出现：SQLAlchemy 渲染 CREATE TABLE 时会把函数默认值
# 包成 ``(now())``，渲染 ``ALTER TABLE ... CHANGE`` 时却原样输出 ``now()``。
# 所以迁移里必须手写 ``sa.text("(now())")``，不能图省事写 ``sa.func.now()``。
#
# ORM 侧（:data:`app.models.run_approval.RunApproval.requested_at`）用
# ``sa.func.now()`` 是安全的——它只出现在 CREATE TABLE 里，渲染结果同样是
# ``(now())``，两边因此对齐。
MYSQL_NOW_DEFAULT = sa.text("(now())")


def upgrade() -> None:
    """加 attempts_json 列；MySQL 上顺带修 run_approval 的时间列精度。"""
    with op.batch_alter_table("run_step") as batch:
        batch.add_column(sa.Column("attempts_json", sa.JSON(), nullable=True))

    if op.get_context().dialect.name == "mysql":
        with op.batch_alter_table("run_approval") as batch:
            batch.alter_column(
                "requested_at",
                existing_type=sa.DateTime(),
                type_=DATETIME3,
                existing_nullable=False,
                existing_server_default=MYSQL_NOW_DEFAULT,
            )
            batch.alter_column(
                "decided_at",
                existing_type=sa.DateTime(),
                type_=DATETIME3,
                existing_nullable=True,
            )


def downgrade() -> None:
    """删 attempts_json 列；MySQL 上把时间列退回 fsp=0。"""
    if op.get_context().dialect.name == "mysql":
        with op.batch_alter_table("run_approval") as batch:
            batch.alter_column(
                "requested_at",
                existing_type=DATETIME3,
                type_=sa.DateTime(),
                existing_nullable=False,
                existing_server_default=MYSQL_NOW_DEFAULT,
            )
            batch.alter_column(
                "decided_at",
                existing_type=DATETIME3,
                type_=sa.DateTime(),
                existing_nullable=True,
            )

    with op.batch_alter_table("run_step") as batch:
        batch.drop_column("attempts_json")
