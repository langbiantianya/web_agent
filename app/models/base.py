"""SQLAlchemy 声明式基类与共享列类型。"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# MySQL 上使用 DATETIME(3) 毫秒精度，其它方言（SQLite / PostgreSQL）回落到通用
# DateTime——PG 上它渲染成 TIMESTAMP WITHOUT TIME ZONE，精度是**微秒**而不是毫秒。
# 这点差异无害：本项目的 Python 侧写入一律截断到毫秒（见 :func:`utcnow`），
# 而排序从不信时间（靠自增 id 与 run_event.seq），多出来的微秒精度只会让
# 「同一毫秒内的两条事件」更容易区分。
DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")

# 主键：MySQL 用 BIGINT AUTO_INCREMENT；SQLite 只有 INTEGER PRIMARY KEY 才是
# rowid 别名、才会自增，因此这里给 SQLite 方言回落成 Integer。
# PostgreSQL 不需要变体：SQLAlchemy 的 PG 方言会把自增的 BigInteger 主键渲染成
# BIGSERIAL（自带序列），已实测确认。
PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")

# 各库通用的 JSON 列：MySQL → JSON，SQLite → TEXT（SQLAlchemy 的通用 JSON 在
# SQLite 上落成 TEXT），PostgreSQL → 原生 JSON ✓。三种方言都能存同一份结构。
JSON_COL = sa.JSON


def utcnow() -> datetime:
    """取当前 **UTC** 时间，微秒截断到毫秒，对齐 MySQL ``DATETIME(3)`` 的存储精度。

    **返回 naive datetime 而非 aware**：库里存的是裸时间戳，没有时区信息。
    若这里返回 aware 对象，SQLAlchemy 往 MySQL 写的时候会自动转换，反而多一层
    容易出错的时区换算。所有时间列一律用 UTC 裸值。

    精度截断意味着同毫秒内产生的事件可能拿到相同时间戳——**排序请依赖自增 id
    或 ``run_event.seq``，不要依赖时间**。

    注意本函数**不是**当前的时间来源：模型上的时间列用的是
    ``server_default=sa.func.now()``，由数据库自己填。要保证那条路也是 UTC，
    靠的是 :func:`app.config.db._pin_session_timezone` 把 MySQL 会话时区钉成
    ``+00:00``；本函数只服务于 Python 侧自己写入时间戳的场合（例如 run 的
    started_at / finished_at）。
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)
