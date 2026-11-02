"""Skill 数据模型（设计文档 §22.5）。"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.base import Base
from app.models.skill import Skill, SkillResource, SkillRevision


def test_skill_table_columns():
    """skill 表要有设计文档 §22.5 约定的那些列。"""
    cols = set(Base.metadata.tables["skill"].columns.keys())
    assert {
        "id",
        "code",
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed_tools",
        "body",
        "origin",
        "content_hash",
        "enabled",
        "version",
        "created_at",
        "updated_at",
    } <= cols


def test_skill_code_is_unique():
    """code 是业务主键——重复的 code 会让「按 code 取技能」变得不确定。"""
    tbl = Base.metadata.tables["skill"]
    assert any(
        {c.name for c in u.columns} == {"code"} for u in tbl.constraints
        if isinstance(u, sa.UniqueConstraint)
    )


def test_skill_resource_unique_path():
    """同一技能下 path 唯一——它是查表的键，重复了就没有「唯一命中」可言。"""
    tbl = Base.metadata.tables["skill_resource"]
    uniques = [c for c in tbl.constraints if isinstance(c, sa.UniqueConstraint)]
    assert any({c.name for c in u.columns} == {"skill_id", "path"} for u in uniques)


def test_skill_revision_unique_version():
    """同一技能下 version 唯一。"""
    tbl = Base.metadata.tables["skill_revision"]
    uniques = [c for c in tbl.constraints if isinstance(c, sa.UniqueConstraint)]
    assert any({c.name for c in u.columns} == {"skill_id", "version"} for u in uniques)


def test_skill_content_column_is_text():
    """正文用 Text 而非 String——技能正文可以到 20000 字符。"""
    assert isinstance(Base.metadata.tables["skill"].columns["body"].type, sa.Text)


def test_skill_resource_content_is_binary():
    """资源内容是二进制列——资源可能是图片、表格，不是纯文本。"""
    assert isinstance(
        Base.metadata.tables["skill_resource"].columns["content"].type,
        sa.LargeBinary,
    )


def test_orm_classes_importable():
    """三个模型都要能被导入——__init__.py 汇总导出。"""
    assert Skill.__tablename__ == "skill"
    assert SkillResource.__tablename__ == "skill_resource"
    assert SkillRevision.__tablename__ == "skill_revision"
