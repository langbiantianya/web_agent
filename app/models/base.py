"""SQLAlchemy 声明式基类与共享列类型。"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# MySQL 上使用 DATETIME(3) 毫秒精度，其它方言（SQLite）回落到通用 DateTime
DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")

# 主键：MySQL 用 BIGINT AUTO_INCREMENT；SQLite 只有 INTEGER PRIMARY KEY 才是
# rowid 别名、才会自增，因此这里给 SQLite 方言回落成 Integer。
PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")

# 各库通用的 JSON 列：MySQL → JSON，SQLite → TEXT
JSON_COL = sa.JSON


def utcnow() -> datetime:
    """取当前时间并把微秒截断到毫秒，对齐 MySQL ``DATETIME(3)`` 的存储精度。

    坑：函数名带 utc，实际取的是服务器本地时间（各节点时区必须一致，
    否则事件时间线会错乱）；截断精度也意味着同毫秒内产生的事件可能拿到
    相同时间戳，排序请依赖自增 id 或 run_event.seq 而不是时间。
    """
    return datetime.now().replace(microsecond=(datetime.now().microsecond // 1000) * 1000)
