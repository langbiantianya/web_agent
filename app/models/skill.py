"""Skill 相关表（设计文档 §22.5，V4）。

技能内容**全部存在数据库里**——没有「部署目录」这回事（§22.4）。``SKILL.md``
是**导出格式**而不是存储格式：frontmatter 拆成列，正文存在 ``skill.body``，
资源文件一行一条存在 ``skill_resource``。

三张表的分工：

- ``skill``：技能定义（frontmatter 字段 + 正文 + 指纹 + 状态）；
- ``skill_resource``：技能的资源文件（``references/`` ``assets/`` ``scripts/``）；
- ``skill_revision``：变更审计——技能进了 DB 就没有 git 了，改错了要能回滚。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base

# 资源内容列。MySQL 上必须显式用 LONGBLOB：SQLAlchemy 的 ``LargeBinary`` 在
# MySQL 上只映射到 ``BLOB``（64 KB 上限），而技能包里放个图片就可能超。
# 其它方言回落成 ``LargeBinary``：PG → BYTEA，SQLite → BLOB。
LONGBLOB = sa.LargeBinary().with_variant(mysql_dialect.LONGBLOB(), "mysql")


class Skill(Base):
    """技能定义。"""

    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # 技能的业务主键。与 frontmatter 的 name 必须一致（§22.5）。
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    description: Mapped[str] = mapped_column(sa.String(1024), default="")
    license: Mapped[str] = mapped_column(sa.String(128), default="")
    compatibility: Mapped[str] = mapped_column(sa.String(500), default="")
    # 列名是 metadata（frontmatter 的字段名），但 ORM 属性必须叫 metadata_：
    # ``metadata`` 是 SQLAlchemy 声明式基类的保留名，用它会直接覆盖 Base.metadata。
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON_COL, default=None)
    # frontmatter 的 allowed-tools 原样存。**仅建议**，不用于授权（§22.9）。
    allowed_tools: Mapped[str] = mapped_column(sa.String(512), default="")
    # SKILL.md 的正文（frontmatter 之外的部分）。
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    # manual | import。导入的技能默认 enabled=0（§22.10）。
    origin: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="manual")
    # sha256(body ‖ 各资源按 path 排序的摘要)——管变更检测与脚本执行的幂等键。
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(sa.SmallInteger, default=1)
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
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
        sa.UniqueConstraint("code", name="uq_skill_code"),
        sa.Index("idx_skill_enabled", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。"""
        return f"<Skill {self.code} v{self.version}>"


class SkillResource(Base):
    """技能的一个资源文件。"""

    __tablename__ = "skill_resource"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # 相对技能根的路径，如 ``references/METRICS.md``。与 skill_id 组成唯一键。
    path: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="application/octet-stream"
    )
    # 文本资源才进提示词、才参与回滚（§22.12 第 7 条）。
    is_text: Mapped[bool] = mapped_column(sa.SmallInteger, default=1)
    content: Mapped[bytes] = mapped_column(LONGBLOB, nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint("skill_id", "path", name="uk_skill_path"),
        sa.Index("idx_skill_resource_skill", "skill_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。"""
        return f"<SkillResource {self.path} ({self.size_bytes}B)>"


class SkillRevision(Base):
    """技能的一次变更快照（审计 + 回滚）。"""

    __tablename__ = "skill_revision"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    # 资源清单：[{path, sha256, is_text, text?}]。**文本资源带内容，二进制只带摘要**
    # ——所以文本资源能回滚、二进制不能（§22.12 第 7 条）。
    resources: Mapped[list | None] = mapped_column(JSON_COL, default=None)
    author: Mapped[str] = mapped_column(sa.String(64), default="")
    note: Mapped[str] = mapped_column(sa.String(255), default="")
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("skill_id", "version", name="uk_skill_version"),
        sa.Index("idx_skill_revision_skill", "skill_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。"""
        return f"<SkillRevision skill={self.skill_id} v{self.version}>"
