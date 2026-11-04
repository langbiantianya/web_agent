# Skill 基础（V4 Phase 1）Implementation Plan

> 历史实施记录：本文保留当时任务，不是当前 TODO。当前待办见
> [设计 TODO](../../design/TODO.md)。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Skill 从「部署目录」搬进数据库，落成可 CRUD、可导入导出、可审计、可注入提示词、可被模型按需读资源的一套完整能力——**不依赖 MCP，也不依赖 studio**。

**Architecture:** 三张表（`skill` / `skill_resource` / `skill_revision`）承载技能内容与审计；纯函数层负责路径格式校验与 `SKILL.md` 解析/拼装；`skill_context` 负责提示词注入的预算与截断；内置工具 `skill_resource` 让模型按需读资源；路由层提供读写两套接口，写接口受 admin 门禁保护。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / Alembic / PyYAML（frontmatter 解析）/ pytest-asyncio（`asyncio_mode = auto`）

## 全局约束

- **三方方言**：MySQL / PostgreSQL / SQLite 同一份代码。建表主键必须写 `sa.BigInteger().with_variant(sa.Integer, "sqlite")`（SQLite 只有 `INTEGER PRIMARY KEY` 才自增）。
- **JSON 列**用 `app.models.base.JSON_COL`；**毫秒时间列**用 `DATETIME3`。
- **硬编码上限**（新增，集中在这几个常量里）：单资源 5 MB、单技能资源总量 20 MB、技能正文 20000 字符、技能正文总预算 4000 字符、注入技能数 5、资源路径 255 字符 / 8 段。
- **读宽松、写严格**：写路径校验失败一律 400；读路径遇到未知技能 code 一律丢弃，**绝不让 Agent 列表接口 500**。
- **admin 门禁 fail-closed**：`ADMIN_KEY` 未配置 → 写接口一律 403，不放行。
- **注释与 docstring 用中文**，解释「为什么」而不是「做了什么」——与 `app/core/tools.py`、`app/core/arith.py` 同一风格。
- **不改** `app/core/agent_runner.py` 的工具闭环；`skill_resource` 只是一个新的内置工具，走现有注册表。

## Phase 拆分说明

V4 覆盖三个相对独立的子系统，各自出计划、各自可交付：

| Phase | 内容 | 计划 |
| --- | --- | --- |
| **Phase 1（本计划）** | Skill 基础：表 + CRUD + 导入导出 + 审计 + 提示词注入 + `skill_resource` 工具 | 本文件 |
| Phase 2 | MCP 客户端：连接管理、工具目录快照、`mcp__` 命名分发、API | 待 Phase 1 落地后写 |
| Phase 3 | studio 服务 + `skill_exec`：proto、gRPC 客户端、容器部署 | 待 Phase 2 落地后写 |

Phase 3 需要 `grpcio`（**当前 venv 未安装**，`requirements.txt` 也没有）；Phase 1 不需要。

---

### Task 1: 数据模型与迁移

**Files:**
- Create: `app/models/skill.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/0005_skill.py`
- Test: `tests/test_skill_models.py`

**Interfaces:**
- Produces: `Skill`、`SkillResource`、`SkillRevision` 三个 ORM 类（字段见下），供 Task 3 的仓储层与 Task 8 的 schema 使用。

- [ ] **Step 1: 写失败的测试**

`tests/test_skill_models.py`：

```python
"""Skill 数据模型（设计文档 §22.5）。"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.base import Base
from app.models.skill import Skill, SkillResource, SkillRevision


def test_skill_table_columns():
    """skill 表要有设计文档 §22.5 约定的那些列。"""
    cols = set(Base.metadata.tables["skill"].columns.keys())
    assert {
        "id", "code", "name", "description", "license", "compatibility",
        "metadata", "allowed_tools", "body", "origin", "content_hash",
        "enabled", "version", "created_at", "updated_at",
    } <= cols


def test_skill_resource_unique_path():
    """同一技能下 path 唯一——它是查表的键，重复了就没有「唯一命中」可言。"""
    tbl = Base.metadata.tables["skill_resource"]
    uniques = [c for c in tbl.constraints if isinstance(c, sa.UniqueConstraint)]
    assert any(
        {c.name for c in u.columns} == {"skill_id", "path"} for u in uniques
    )


def test_skill_revision_unique_version():
    """同一技能下 version 唯一。"""
    tbl = Base.metadata.tables["skill_revision"]
    uniques = [c for c in tbl.constraints if isinstance(c, sa.UniqueConstraint)]
    assert any({c.name for c in u.columns} == {"skill_id", "version"} for u in uniques)


def test_skill_content_column_is_text():
    """正文用 Text 而非 String——技能正文可以到 20000 字符。"""
    assert isinstance(Base.metadata.tables["skill"].columns["body"].type, sa.Text)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_skill_models.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.models.skill'`

- [ ] **Step 3: 写模型**

`app/models/skill.py`：

```python
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSON_COL, PK, Base

# 资源路径的字符集白名单。刻意比文件系统保守：只有字母、数字、下划线、连字符、
# 点与斜杠——没有空格、没有中文、没有 ``:``（Windows 非法）、没有 ``%``。
# 路径是查表的键，字符集越窄，误伤与歧义越少。
RESOURCE_PATH_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"


class Skill(Base):
    """技能定义。"""

    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # 技能的业务主键。与 frontmatter 的 name 必须一致（§22.5）。
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    description: Mapped[str] = mapped_column(sa.String(1024), default="")
    license: Mapped[str] = mapped_column(sa.String(128), default="")
    compatibility: Mapped[str] = mapped_column(sa.String(500), default="")
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSON_COL, default=None
    )
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
        sa.DateTime, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    __table_args__ = (sa.Index("idx_skill_enabled", "enabled"),)

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
    content: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    updated_at: Mapped[object] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
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
        sa.DateTime, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("skill_id", "version", name="uk_skill_version"),
        sa.Index("idx_skill_revision_skill", "skill_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。"""
        return f"<SkillRevision skill={self.skill_id} v{self.version}>"
```

- [ ] **Step 4: 注册模型**

`app/models/__init__.py` 里追加（保持与现有 `from app.models.workflow import Workflow` 同样的风格）：

```python
from app.models.skill import Skill, SkillResource, SkillRevision
```

并在 `__all__` 里补上这三个名字。

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_skill_models.py -v`
Expected: PASS（4 passed）

- [ ] **Step 6: 写迁移**

`alembic/versions/0005_skill.py`，`down_revision` 指向 `0004_run_step_attempts.py` 的 revision id（**先读那个文件确认，不要猜**），`revision` 用一个新的 12 位十六进制串。

迁移必须手写 `op.create_table`（不靠 autogenerate），主键写成：

```python
sa.BigInteger().with_variant(sa.Integer, "sqlite")
```

时间列用 `DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")`——与 `app.models.base` 保持一致，否则 autogenerate 会永远报一条多余的 ALTER 差异。

`downgrade()` 里 `op.drop_table` 三张表（先删有外键语义的两张：skill_resource、skill_revision，最后删 skill）。

- [ ] **Step 7: 跑迁移测试**

Run: `pytest tests/test_migrations.py -v`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add app/models/skill.py app/models/__init__.py alembic/versions/0005_skill.py tests/test_skill_models.py
git commit -m "feat(skill): 技能数据模型与迁移"
```

---

### Task 2: 纯函数层——路径校验与 SKILL.md 解析

**Files:**
- Create: `app/core/skill_paths.py`
- Create: `app/core/skill_pack.py`
- Test: `tests/test_skill_pack.py`

**Interfaces:**
- Produces:
  - `skill_paths.validate_resource_path(path: str) -> str` — 校验并返回规整后的路径；不合规抛 `SkillPathError`（`ValueError` 子类）。
  - `skill_paths.MAX_PATH_LEN = 255`、`skill_paths.MAX_PATH_DEPTH = 8`
  - `skill_pack.SkillPackError(Exception)`
  - `skill_pack.SkillFrontmatter`（frozen dataclass）：`name: str`、`description: str`、`license: str = ""`、`compatibility: str = ""`、`metadata: dict = {}`、`allowed_tools: str = ""`
  - `skill_pack.ParsedSkill`（frozen dataclass）：`frontmatter: SkillFrontmatter`、`body: str`
  - `skill_pack.parse_skill_md(text: str) -> ParsedSkill`
  - `skill_pack.render_skill_md(fm: SkillFrontmatter, body: str) -> str`
  - `skill_pack.compute_content_hash(body: str, resources: list[tuple[str, bytes]]) -> str`
  - `skill_pack.guess_mime_type(path: str) -> str`、`skill_pack.is_text_path(path: str) -> bool`

- [ ] **Step 1: 写失败的测试**

`tests/test_skill_pack.py`（路径校验 + 解析 + 指纹三组）：

```python
"""技能路径校验与 SKILL.md 解析（设计文档 §22.4、§22.7）。"""

from __future__ import annotations

import pytest

from app.core.skill_pack import (
    SkillPackError,
    compute_content_hash,
    parse_skill_md,
    render_skill_md,
)
from app.core.skill_paths import SkillPathError, validate_resource_path


# ------------------------------------------------------------- 路径校验


@pytest.mark.parametrize(
    "path",
    [
        "references/METRICS.md",
        "METRICS.md",
        "references/deep/nested/file.md",
        "a-b_c.1.txt",
    ],
)
def test_valid_paths_accepted(path):
    """正常路径原样通过。"""
    assert validate_resource_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",          # 绝对路径
        "../../etc/passwd",     # 上跳
        "a/../b",               # 中间上跳
        "a//b",                 # 空段
        "a\\b",                 # Windows 分隔符
        "a b.md",               # 空格
        "a/b.md/",              # 结尾斜杠 → 空段
        "x" * 256,              # 超长
    ],
)
def test_invalid_paths_rejected(path):
    """非法路径一律拒——路径是查表的键，宁可拒得严。"""
    with pytest.raises(SkillPathError):
        validate_resource_path(path)


def test_path_depth_limit():
    """段数上限 8。"""
    deep = "/".join(f"d{i}" for i in range(9))
    with pytest.raises(SkillPathError):
        validate_resource_path(deep)


# --------------------------------------------------------- SKILL.md 解析


def test_parse_skill_md():
    """frontmatter 拆成字段，正文留在 body。"""
    text = (
        "---\n"
        "name: financial-report\n"
        "description: 生成季度财务分析报告。\n"
        "license: Apache-2.0\n"
        "metadata:\n"
        "  author: finance-team\n"
        "  version: \"1.0\"\n"
        "---\n"
        "\n"
        "# 标题\n\n正文。\n"
    )
    parsed = parse_skill_md(text)
    assert parsed.frontmatter.name == "financial-report"
    assert parsed.frontmatter.description == "生成季度财务分析报告。"
    assert parsed.frontmatter.license == "Apache-2.0"
    assert parsed.frontmatter.metadata == {"author": "finance-team", "version": "1.0"}
    assert parsed.body.strip() == "# 标题\n\n正文。"


def test_parse_skill_md_missing_frontmatter():
    """没有 frontmatter → 报错，不猜。"""
    with pytest.raises(SkillPackError):
        parse_skill_md("# 只有正文\n")


def test_parse_skill_md_missing_required_field():
    """缺 name 或 description → 报错。"""
    with pytest.raises(SkillPackError):
        parse_skill_md("---\ndescription: 缺 name\n---\n正文\n")
    with pytest.raises(SkillPackError):
        parse_skill_md("---\nname: x\n---\n正文\n")


def test_parse_skill_md_rejects_uppercase_name():
    """name 含大写 → 报错（规范要求小写字母/数字/连字符）。"""
    with pytest.raises(SkillPackError):
        parse_skill_md("---\nname: PDF-Report\ndescription: d\n---\n正文\n")


def test_render_roundtrip():
    """导出再解析，正文与 frontmatter 都要能回来——导入导出必须对称。"""
    text = (
        "---\n"
        "name: demo\n"
        "description: 演示技能。\n"
        "---\n"
        "\n"
        "正文内容。\n"
    )
    parsed = parse_skill_md(text)
    rendered = render_skill_md(parsed.frontmatter, parsed.body)
    again = parse_skill_md(rendered)
    assert again.frontmatter.name == parsed.frontmatter.name
    assert again.frontmatter.description == parsed.frontmatter.description
    assert again.body.strip() == parsed.body.strip()


# ------------------------------------------------------------- 内容指纹


def test_content_hash_stable_and_order_independent():
    """指纹与资源顺序无关，与内容有关。"""
    a = compute_content_hash("body", [("a.md", b"x"), ("b.md", b"y")])
    b = compute_content_hash("body", [("b.md", b"y"), ("a.md", b"x")])
    c = compute_content_hash("body", [("a.md", b"x"), ("b.md", b"z")])
    d = compute_content_hash("other", [("a.md", b"x"), ("b.md", b"y")])
    assert a == b
    assert a != c
    assert a != d
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_skill_pack.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.core.skill_paths'`

- [ ] **Step 3: 写 `app/core/skill_paths.py`**

```python
"""技能资源路径的格式校验（设计文档 §22.7）。

## 为什么还有校验

技能进了数据库之后**没有任何目录可解析**——``path`` 是一个查表的键，不是一条路径。
``../../etc/passwd`` 查不到任何行就什么也读不到，所以「路径穿越」这一类漏洞
在存储形态改变的同时就消失了（§22.7）。

但格式校验仍然保留，理由有三：
1. **挡脏数据**：不合规的 path 不该进库；
2. **挡无意义的查询**：``../../x`` 这种路径查库必然落空，早拒比跑一趟 SQL 便宜；
3. **写入期与读取期同规则**：写的时候严，读的时候才敢宽。

这不是「防穿越」了，是**输入规整**。
"""

from __future__ import annotations

import re

# 路径长度上限。与 skill_resource.path 的列宽一致（VARCHAR(255)）。
MAX_PATH_LEN = 255
# 路径段数上限。技能包里的引用通常只有一两层，八段足够，也顺手挡掉畸形输入。
MAX_PATH_DEPTH = 8

# 字符集白名单：字母、数字、下划线、连字符、点、斜杠。
# 没有空格（URL 与文件系统里都要转义）、没有中文（避免编码与大小写折叠的坑）、
# 没有 ``:``（Windows 非法）、没有 ``%``（百分号编码歧义）。
_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class SkillPathError(ValueError):
    """资源路径不合规。"""


def validate_resource_path(path: str) -> str:
    """校验资源路径格式，返回规整后的路径。

    Args:
        path: 待校验的相对路径，如 ``references/METRICS.md``。

    Returns:
        去掉首尾空白后的路径。

    Raises:
        SkillPathError: 路径为空、超长、含非法字符、含上跳段或段数超限。
    """
    if not isinstance(path, str):
        raise SkillPathError("资源路径必须是字符串")
    cleaned = path.strip()
    if not cleaned:
        raise SkillPathError("资源路径不能为空")
    if len(cleaned) > MAX_PATH_LEN:
        raise SkillPathError(f"资源路径超过 {MAX_PATH_LEN} 字符")
    if cleaned.startswith("/"):
        raise SkillPathError("资源路径必须是相对路径，不能以 / 开头")
    if "\\" in cleaned:
        raise SkillPathError("资源路径不能包含反斜杠")
    if not _PATH_RE.match(cleaned):
        raise SkillPathError("资源路径只能包含字母、数字、下划线、连字符、点与斜杠")
    segments = cleaned.split("/")
    if len(segments) > MAX_PATH_DEPTH:
        raise SkillPathError(f"资源路径层数超过 {MAX_PATH_DEPTH}")
    if any(seg == "" for seg in segments):
        raise SkillPathError("资源路径不能有空段")
    if any(seg == ".." for seg in segments):
        raise SkillPathError("资源路径不能包含 .. ")
    return cleaned
```

- [ ] **Step 4: 写 `app/core/skill_pack.py`**

```python
"""SKILL.md 的解析 / 拼装 / 指纹（设计文档 §22.4、§22.5）。

``SKILL.md`` 是 Agent Skills 开放格式里的**导出格式**：frontmatter + Markdown 正文。
本项目把它拆成「DB 的若干列 + 一段 body」，于是需要一对互逆的函数：

- :func:`parse_skill_md` —— 导出物 → 结构化字段（导入时用）；
- :func:`render_skill_md` —— 结构化字段 → 导出物（导出时用）。

两者**必须互逆**（:func:`test_render_roundtrip` 钉住这件事）：导出后能原样导回去，
才叫「可移植」。
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field

import yaml

# frontmatter 里必须有的字段。
_REQUIRED_FIELDS = ("name", "description")
# name 的约束（规范原文）：小写字母/数字/连字符，不以连字符开头结尾，不连续连字符。
_NAME_MAX = 64
_DESCRIPTION_MAX = 1024
_COMPATIBILITY_MAX = 500


class SkillPackError(Exception):
    """SKILL.md 解析失败——格式不对或缺必填字段。"""


@dataclass(frozen=True)
class SkillFrontmatter:
    """SKILL.md 的 frontmatter 字段。"""

    name: str
    description: str
    license: str = ""
    compatibility: str = ""
    metadata: dict = field(default_factory=dict)
    allowed_tools: str = ""


@dataclass(frozen=True)
class ParsedSkill:
    """一个技能包解析后的结果。"""

    frontmatter: SkillFrontmatter
    body: str


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 frontmatter 的 dict 与正文。

    Args:
        text: SKILL.md 原文。

    Returns:
        ``(frontmatter_dict, body)``。

    Raises:
        SkillPackError: 没有 frontmatter，或 YAML 解析失败。
    """
    if not text.startswith("---"):
        raise SkillPackError("SKILL.md 缺少 YAML frontmatter")
    # 第一行是 '---'，找下一个独占一行的 '---'。
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        raise SkillPackError("SKILL.md 的 frontmatter 没有闭合")
    raw = parts[0][len("---"):]
    body = parts[1]
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise SkillPackError(f"frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise SkillPackError("frontmatter 必须是键值映射")
    return data, body


def parse_skill_md(text: str) -> ParsedSkill:
    """解析 SKILL.md 原文。

    Args:
        text: SKILL.md 原文。

    Returns:
        :class:`ParsedSkill`。

    Raises:
        SkillPackError: 缺 frontmatter、缺必填字段、字段值不合法。
    """
    data, body = _split_frontmatter(text)
    for key in _REQUIRED_FIELDS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SkillPackError(f"frontmatter 缺少必填字段 {key}")
    name = data["name"].strip()
    if len(name) > _NAME_MAX:
        raise SkillPackError(f"name 超过 {_NAME_MAX} 字符")
    if name.startswith("-") or name.endswith("-") or "--" in name:
        raise SkillPackError("name 不能以连字符开头结尾，也不能有连续连字符")
    if not all(c.islower() or c.isdigit() or c == "-" for c in name):
        raise SkillPackError("name 只能包含小写字母、数字与连字符")
    description = data["description"].strip()
    if len(description) > _DESCRIPTION_MAX:
        raise SkillPackError(f"description 超过 {_DESCRIPTION_MAX} 字符")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise SkillPackError("metadata 必须是键值映射")
    compatibility = str(data.get("compatibility") or "")
    if len(compatibility) > _COMPATIBILITY_MAX:
        raise SkillPackError(f"compatibility 超过 {_COMPATIBILITY_MAX} 字符")
    allowed_tools = data.get("allowed-tools") or ""
    if not isinstance(allowed_tools, str):
        raise SkillPackError("allowed-tools 必须是字符串")
    return ParsedSkill(
        frontmatter=SkillFrontmatter(
            name=name,
            description=description,
            license=str(data.get("license") or ""),
            compatibility=compatibility,
            metadata={str(k): str(v) for k, v in metadata.items()},
            allowed_tools=allowed_tools.strip(),
        ),
        body=body.lstrip("\n"),
    )


def render_skill_md(fm: SkillFrontmatter, body: str) -> str:
    """把结构化字段拼回标准 SKILL.md 原文。

    Args:
        fm: frontmatter 字段。
        body: Markdown 正文。

    Returns:
        SKILL.md 原文（以换行结尾）。
    """
    lines = ["---", f"name: {fm.name}", f"description: {fm.description}"]
    if fm.license:
        lines.append(f"license: {fm.license}")
    if fm.compatibility:
        lines.append(f"compatibility: {fm.compatibility}")
    if fm.metadata:
        lines.append("metadata:")
        for key, value in fm.metadata.items():
            lines.append(f"  {key}: {value}")
    if fm.allowed_tools:
        lines.append(f"allowed-tools: {fm.allowed_tools}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body.rstrip("\n") + "\n"


def compute_content_hash(body: str, resources: list[tuple[str, bytes]]) -> str:
    """算技能的内容指纹。

    口径：``sha256(body ‖ 各资源按 path 排序后的 (path ‖ sha256(content)))``。
    资源顺序无关，内容变则指纹变。

    Args:
        body: 技能正文。
        resources: ``[(path, content), ...]``。

    Returns:
        64 位十六进制 sha256。
    """
    digest = hashlib.sha256()
    digest.update(body.encode("utf-8"))
    for path, content in sorted(resources, key=lambda item: item[0]):
        digest.update(path.encode("utf-8"))
        digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
    return digest.hexdigest()


def guess_mime_type(path: str) -> str:
    """按扩展名猜 mime 类型；猜不到就给二进制。"""
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def is_text_path(path: str) -> bool:
    """判断这个路径下的文件是不是文本。

    决定它能不能进提示词、能不能参与回滚（§22.12 第 7 条）。
    """
    mime = guess_mime_type(path)
    if mime.startswith("text/"):
        return True
    return mime in (
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "application/yaml",
    )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_skill_pack.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/core/skill_paths.py app/core/skill_pack.py tests/test_skill_pack.py
git commit -m "feat(skill): 资源路径校验与 SKILL.md 解析"
```

---

### Task 3: 仓储层

**Files:**
- Create: `app/repo/skill_repo.py`
- Test: `tests/test_skill_repo.py`

**Interfaces:**
- Consumes: Task 1 的 `Skill` / `SkillResource` / `SkillRevision`。
- Produces: 模块级单例 `skill_repo`，方法签名见下。

```python
class SkillRepo:
    async def create(self, db, **kwargs) -> Skill
    async def get_by_code(self, db, code) -> Skill | None
    async def list(self, db, only_enabled=False, limit=200, offset=0) -> list[Skill]
    async def update(self, db, code, fields, bump_version=True) -> Skill | None
    async def delete(self, db, code) -> bool
    async def list_resources(self, db, skill_id) -> list[SkillResource]
    async def get_resource(self, db, skill_id, path) -> SkillResource | None
    async def put_resource(self, db, skill_id, path, content, mime_type, is_text) -> SkillResource
    async def delete_resource(self, db, skill_id, path) -> bool
    async def total_resource_bytes(self, db, skill_id, exclude_path=None) -> int
    async def add_revision(self, db, skill_id, version, body, resources, author, note) -> SkillRevision
    async def list_revisions(self, db, skill_id) -> list[SkillRevision]
    async def get_revision(self, db, skill_id, version) -> SkillRevision | None
    async def count_agents_using(self, db, code) -> int
```

- [ ] **Step 1: 写失败的测试**

`tests/test_skill_repo.py`，用 `db` fixture（**先读 `tests/conftest.py` 确认现成的 db fixture 叫什么**，别自己造）：

```python
"""Skill 仓储层（设计文档 §22.5）。"""

from __future__ import annotations

import pytest

from app.repo.skill_repo import skill_repo


async def test_create_and_get(db):
    """建了能按 code 取回。"""
    sk = await skill_repo.create(
        db, code="demo", name="demo", description="演示", body="正文",
        content_hash="h", origin="manual",
    )
    got = await skill_repo.get_by_code(db, "demo")
    assert got is not None
    assert got.id == sk.id
    assert got.body == "正文"


async def test_update_bumps_version(db):
    """更新时 version 原子 +1（与 workflow 同口径）。"""
    await skill_repo.create(
        db, code="demo", name="demo", description="d", body="b", content_hash="h",
    )
    updated = await skill_repo.update(db, "demo", {"body": "新正文"})
    assert updated is not None
    assert updated.version == 2
    assert updated.body == "新正文"


async def test_put_resource_is_upsert(db):
    """同一个 path 写两次是覆盖，不是新增。"""
    sk = await skill_repo.create(
        db, code="demo", name="demo", description="d", body="b", content_hash="h",
    )
    await skill_repo.put_resource(db, sk.id, "a.md", b"one", "text/markdown", 1)
    await skill_repo.put_resource(db, sk.id, "a.md", b"two", "text/markdown", 1)
    rows = await skill_repo.list_resources(db, sk.id)
    assert len(rows) == 1
    assert rows[0].content == b"two"


async def test_delete_cascades_resources(db):
    """删技能要连资源与修订历史一起删——不留孤儿行。"""
    sk = await skill_repo.create(
        db, code="demo", name="demo", description="d", body="b", content_hash="h",
    )
    await skill_repo.put_resource(db, sk.id, "a.md", b"one", "text/markdown", 1)
    await skill_repo.add_revision(db, sk.id, 1, "b", [], "admin", "init")
    assert await skill_repo.delete(db, "demo") is True
    assert await skill_repo.get_by_code(db, "demo") is None
    assert await skill_repo.list_resources(db, sk.id) == []
    assert await skill_repo.list_revisions(db, sk.id) == []


async def test_count_agents_using(db):
    """统计有几个 Agent 绑着这个技能。"""
    assert await skill_repo.count_agents_using(db, "demo") == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_skill_repo.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.repo.skill_repo'`

- [ ] **Step 3: 写 `app/repo/skill_repo.py`**

按 `app/repo/workflow_repo.py` 的同款风格写（类 + 模块级单例，异步方法，`db: AsyncSession` 首参）。要点：

- `update()` 的版本自增用 **SQL 表达式** `version=Skill.version + 1`，不要「读出来 +1 再写回」——并发下会丢版本（workflow_repo 里已有同样的注释，照抄那个理由）。
- `put_resource()` 是 upsert：先 `get_resource`，有就改 `content` / `size_bytes` / `mime_type` / `is_text`，没有就 insert。
- `delete()` 手动级联删 `skill_resource` 与 `skill_revision`（不建外键，项目其它表也没建）。
- `count_agents_using()`：查 `agent` 表，把 `skills` JSON 列里的 code 匹配出来计数。用 SQL 层的 JSON 表达式跨库不现实，**取回 agent 列表后在 Python 里数**（Agent 数量是配置级、量级很小，这样三种方言都能跑）。
- `total_resource_bytes()`：`sum(size_bytes)`，用于「单技能资源总量 20 MB」的上限校验。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_skill_repo.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/repo/skill_repo.py tests/test_skill_repo.py
git commit -m "feat(skill): 技能仓储层"
```

---

### Task 4: 提示词注入

**Files:**
- Create: `app/core/skill_context.py`
- Test: `tests/test_skill_context.py`

**Interfaces:**
- Produces:
  - `SkillPrompt`（frozen dataclass）：`code: str`、`name: str`、`version: int`、`body: str`
  - `SkillPromptPlan`（frozen dataclass）：`messages: list[dict]`、`skipped: list[tuple[str, str]]`
  - `build_skill_messages(skills: list[SkillPrompt], budget: int, max_count: int) -> SkillPromptPlan`

- [ ] **Step 1: 写失败的测试**

```python
"""技能正文注入提示词（设计文档 §22.6）。"""

from __future__ import annotations

from app.core.skill_context import SkillPrompt, build_skill_messages


def _skill(code: str, body: str) -> SkillPrompt:
    return SkillPrompt(code=code, name=code, version=1, body=body)


def test_no_skills_yields_no_messages():
    """没绑技能就不该多出一条 system 消息。"""
    plan = build_skill_messages([], budget=4000, max_count=5)
    assert plan.messages == []
    assert plan.skipped == []


def test_skill_becomes_own_system_message():
    """技能正文作为独立的一条 system 消息。"""
    plan = build_skill_messages([_skill("a", "内容 A")], budget=4000, max_count=5)
    assert len(plan.messages) == 1
    assert plan.messages[0]["role"] == "system"
    assert "内容 A" in plan.messages[0]["content"]


def test_budget_overflow_skips_whole_skill():
    """超预算时整个技能不注入，且记录为什么——不静默截断。"""
    skills = [_skill("a", "x" * 100), _skill("b", "y" * 100)]
    plan = build_skill_messages(skills, budget=150, max_count=5)
    assert len(plan.messages) == 1
    assert plan.skipped and plan.skipped[0][0] == "b"


def test_max_count_limit():
    """注入技能数上限。"""
    skills = [_skill(f"s{i}", "x") for i in range(10)]
    plan = build_skill_messages(skills, budget=100000, max_count=3)
    assert len(plan.messages) == 3
    assert len(plan.skipped) == 7
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_skill_context.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.core.skill_context'`

- [ ] **Step 3: 写 `app/core/skill_context.py`**

```python
"""技能正文进提示词的拼装与预算（设计文档 §22.6）。

## 注入形态

Agent Skills 的标准做法是三级渐进披露：发现（name+description）→ 激活（正文）→
执行（按需读文件）。本项目是**配置驱动**的——``agent.skills`` 里点名的技能才会
出现，模型没有「挑技能」这一步，于是「发现」那一级不存在，「绑定即注入」。

## 两条硬规则

1. **独立成条**：技能正文作为一条**独立的** system 消息，不拼进 Agent 的
   ``system_prompt`` 字段。拼进去的话，后台页面上「提示词」那一栏显示的内容就
   和实际发给模型的不一致——运营改提示词时看到的不是模型真正收到的东西。
2. **宁少勿半**：超预算时**整个技能不注入**，不做半个技能的截断。半个 SKILL.md
   里的指令可能自相矛盾（前一半说用 A 方法，被砍掉的后一半说用 B 方法），比不给
   更糟。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillPrompt:
    """一条待注入的技能。"""

    code: str
    name: str
    version: int
    body: str


@dataclass(frozen=True)
class SkillPromptPlan:
    """注入结果。"""

    # 要追加在 Agent 自己的 system_prompt 之后的 system 消息。
    messages: list[dict]
    # 被跳过的技能与原因：(code, reason)。
    skipped: list[tuple[str, str]]


def build_skill_messages(
    skills: list[SkillPrompt], budget: int, max_count: int
) -> SkillPromptPlan:
    """按声明顺序把技能正文拼成 system 消息。

    Args:
        skills: 待注入的技能，顺序即优先级（运营靠顺序表达优先级）。
        budget: 正文总字符预算。
        max_count: 最多注入几个技能。

    Returns:
        :class:`SkillPromptPlan`。``skipped`` 里说明每个被跳过的技能为什么被跳过。
    """
    messages: list[dict] = []
    skipped: list[tuple[str, str]] = []
    used = 0
    for index, skill in enumerate(skills):
        if index >= max_count:
            skipped.append((skill.code, f"超过注入技能数上限（{max_count}）"))
            continue
        cost = len(skill.body)
        if used + cost > budget:
            skipped.append((skill.code, f"超过提示词预算（{budget} 字符）"))
            continue
        used += cost
        messages.append({"role": "system", "content": skill.body})
    return SkillPromptPlan(messages=messages, skipped=skipped)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_skill_context.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/skill_context.py tests/test_skill_context.py
git commit -m "feat(skill): 技能正文注入提示词"
```

---

### Task 5: admin 门禁

**Files:**
- Create: `app/core/admin.py`
- Modify: `app/config/settings.py`
- Modify: `app/api/deps.py`
- Test: `tests/test_admin.py`

**Interfaces:**
- Produces: `app.core.admin.require_admin(key: str | None) -> str` —— 校验通过返回调用方标识（用于审计的 `author`），失败抛 `UnauthorizedError`（401）。

- [ ] **Step 1: 写失败的测试**

```python
"""admin 门禁（设计文档 §22.10 第 1 条）。"""

from __future__ import annotations

import pytest

from app.core.admin import require_admin
from app.core.errors import UnauthorizedError


def test_admin_key_not_configured_is_closed():
    """未配置 ADMIN_KEY 时一律拒——fail-closed。

    这道门后面是「谁能改模型的行为」。默认开着等于没有门。
    """
    with pytest.raises(UnauthorizedError):
        require_admin(None)
    with pytest.raises(UnauthorizedError):
        require_admin("")


def test_admin_key_mismatch_rejected():
    """密钥不对 → 拒。"""
    with pytest.raises(UnauthorizedError):
        require_admin("wrong-key")


def test_admin_key_match_returns_author(monkeypatch):
    """密钥对上 → 返回调用方标识，供审计记 author。"""
    monkeypatch.setattr("app.core.admin.settings", settings_stub())
    assert require_admin("secret-key") == "admin"
```

（`settings_stub()` 用 `types.SimpleNamespace(admin_key="secret-key", admin_author="admin")` 造一个替身 monkeypatch 进去。）

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_admin.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.core.admin'`

- [ ] **Step 3: 写 `app/core/admin.py`**

```python
"""管理员门禁（设计文档 §22.10）。

## 这道门为什么存在

技能正文会进 prompt，**模型会把它当指令执行**。所以「谁能写技能」≈「谁能改模型的
行为」≈「能让任意代码在 studio 容器里跑」。

技能进了数据库之后，「改它要改代码 + 走部署」这道物理阻力没有了——后台就能改。
阻力没了，权限就得从别处补回来，就是这个门。

## 为什么是共享密钥而不是用户体系

本项目目前**没有用户模型**（§1.1：单部署多 Agent，没有登录体系）。为了一个权限位
引入用户表、会话、RBAC 是另一个量级的工程，而且和「技能」这个功能无关。所以这里
用一把**共享密钥**：持有 ``ADMIN_KEY`` 的调用方即管理员。

将来真的有了用户体系，把 :func:`require_admin` 换成按用户查权限即可，调用点不用动。
"""

from __future__ import annotations

import secrets

from app.config.settings import settings
from app.core.errors import UnauthorizedError


def require_admin(key: str | None) -> str:
    """校验管理员密钥。

    Args:
        key: 调用方带上来的密钥（``X-Admin-Key`` 头）。

    Returns:
        管理员标识，写进审计的 ``author``。

    Raises:
        UnauthorizedError: 未配置密钥、没带密钥、密钥不对——三种情况都拒。
    """
    configured = settings.admin_key
    if not configured:
        # fail-closed：没有配置就没有管理员。这道门后面是「改模型的行为」，
        # 默认开着等于没门。
        raise UnauthorizedError(
            "未配置 ADMIN_KEY，技能写接口已禁用（这是有意的：默认关闭）"
        )
    if not key:
        raise UnauthorizedError("缺少管理员密钥")
    if not secrets.compare_digest(key, configured):
        raise UnauthorizedError("管理员密钥不正确")
    return settings.admin_author
```

- [ ] **Step 4: 加配置项**

`app/config/settings.py` 的 `Settings` 类里加：

```python
    # ---- 管理员门禁（V4，§22.10）----
    # 写技能 / 注册 MCP 服务器这类「部署级」操作的凭证。**未配置 = 一律拒绝**
    # （fail-closed）——默认开着等于没有门。
    admin_key: str = ""
    # 审计里记的调用方标识。
    admin_author: str = "admin"
```

- [ ] **Step 5: 加 FastAPI 依赖**

`app/api/deps.py` 里加：

```python
from app.core.admin import require_admin

def admin_key(
    key: Annotated[str | None, Header(alias="X-Admin-Key", description="管理员密钥")] = None,
) -> str:
    """管理员门禁依赖。未配置 ADMIN_KEY 时一律 401（fail-closed）。"""
    return require_admin(key)

AdminDep = Annotated[str, Depends(admin_key)]
```

并把 `AdminDep` 加进 `__all__`。

- [ ] **Step 6: 跑测试确认通过**

Run: `pytest tests/test_admin.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add app/core/admin.py app/config/settings.py app/api/deps.py tests/test_admin.py
git commit -m "feat(admin): 管理员门禁"
```

---

### Task 6: Agent 绑定技能

**Files:**
- Modify: `app/models/agent.py`
- Modify: `app/schemas/agent.py`
- Modify: `app/api/routes/agent.py`
- Create: `app/core/skill_names.py`
- Test: `tests/test_agent_skills.py`

**Interfaces:**
- Produces: `app.core.skill_names.normalize_skill_codes(raw, known: set[str]) -> list[str] | None`（写路径，未知 code 抛 `SkillConfigError`）、`known_skill_codes(raw) -> list[str] | None`（读路径，宽容丢弃未知）。

- [ ] **Step 1: 写失败的测试**

```python
"""Agent 绑定技能（设计文档 §22.5）。"""

from __future__ import annotations

import pytest

from app.core.skill_names import SkillConfigError, known_skill_codes, normalize_skill_codes


def test_normalize_rejects_unknown_code():
    """未注册的技能 code 保存期就拒。"""
    with pytest.raises(SkillConfigError):
        normalize_skill_codes(["nope"], known={"demo"})


def test_normalize_accepts_known():
    """已注册的 code 通过，去重。"""
    assert normalize_skill_codes(["demo", "demo"], known={"demo"}) == ["demo"]


def test_normalize_none():
    """None 表示不用技能。"""
    assert normalize_skill_codes(None, known={"demo"}) is None


def test_known_codes_is_forgiving():
    """读路径宽容：未知 code 丢掉，不能让列表接口 500。"""
    assert known_skill_codes(["demo", "gone"]) == ["demo"]
    assert known_skill_codes("not-a-list") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_skills.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.core.skill_names'`

- [ ] **Step 3: 写 `app/core/skill_names.py`**

```python
"""Agent 的 skills 字段规整（设计文档 §22.5）。

与 :mod:`app.core.tools` 里的 ``normalize_tool_names`` / ``known_tool_names``
**完全同一套口径**：写的时候严（未注册的 code 保存期就拒），读的时候宽（未知 code
丢掉，不能让 Agent 列表接口 500）。

区别只有一处：工具的名字能不能校验，取决于注册表是不是全集；技能的名字能不能校验，
取决于库里有没有这条记录——所以校验需要**传入已知的 code 集合**，而不是查注册表。
"""

from __future__ import annotations

from typing import Any


class SkillConfigError(Exception):
    """agent 的 skills 配置不合法——保存期就拒。"""


def normalize_skill_codes(raw: Any, known: set[str]) -> list[str] | None:
    """把 skills 配置规整成「已注册的技能 code」列表。

    Args:
        raw: 原始配置值（None / 字符串 / 字符串数组）。
        known: 库里已存在且启用的技能 code 集合。

    Returns:
        去重后的 code 列表；没有技能时返回 ``None``。

    Raises:
        SkillConfigError: 不是字符串数组，或含未注册的 code。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise SkillConfigError("skills 必须是技能 code 的数组")

    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise SkillConfigError("skills 里只能放技能 code（字符串）")
        code = item.strip()
        if not code:
            continue
        if code not in known:
            raise SkillConfigError(f"未知技能「{code}」")
        if code not in names:
            names.append(code)
    return names or None


def known_skill_codes(raw: Any) -> list[str] | None:
    """宽容地读出库里存的技能 code——**只读，不报错**。

    Args:
        raw: 库里存的值。

    Returns:
        code 列表；形状不对或全不认识时返回 ``None``。
    """
    if not isinstance(raw, list):
        return None
    names = [n for n in raw if isinstance(n, str) and n.strip()]
    return list(dict.fromkeys(names)) or None
```

- [ ] **Step 4: 加 `skills` 列与 schema**

- `app/models/agent.py`：加 `skills: Mapped[list | None] = mapped_column(JSON_COL, default=None)`（放在 `tools` 之后）。
- `app/schemas/agent.py`：`AgentBase` 加 `skills: list[str] | None = None`；`AgentUpdate` 加 `skills: list[str] | None = None`；`AgentOut` 加 `skills: list[str] | None = None`。

- [ ] **Step 5: 路由层校验**

`app/api/routes/agent.py` 的 create 与 update 里，对 `payload.skills` 做：

```python
from app.core.skill_names import SkillConfigError, normalize_skill_codes
from app.repo.skill_repo import skill_repo

known = {s.code for s in await skill_repo.list(db, only_enabled=True, limit=1000)}
try:
    skills = normalize_skill_codes(payload.skills, known)
except SkillConfigError as exc:
    raise BadRequestError(str(exc)) from exc
```

`SkillConfigError` 不是 `AppError`，要在 `app/api/routes/agent.py` 里显式转成 `BadRequestError`（400）。

- [ ] **Step 6: 跑测试确认通过**

Run: `pytest tests/test_agent_skills.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add app/models/agent.py app/schemas/agent.py app/api/routes/agent.py app/core/skill_names.py tests/test_agent_skills.py
git commit -m "feat(skill): Agent 绑定技能"
```

---

### Task 7: 内置工具 `skill_resource`

**Files:**
- Create: `app/core/skill_reader.py`
- Modify: `app/core/tools.py`
- Test: `tests/test_skill_resource_tool.py`

**Interfaces:**
- Produces: `app.core.skill_reader.read_skill_resource(db, code, path, allowed_codes, max_chars) -> tuple[bool, str]` —— 返回 `(ok, text)`；`allowed_codes` 是该 Agent 绑定的技能 code 集合（`None` 表示不限）。

- [ ] **Step 1: 写失败的测试**

```python
"""skill_resource 工具（设计文档 §22.7）。"""

from __future__ import annotations

import pytest

from app.core.skill_reader import read_skill_resource


async def test_read_registered_resource(db):
    """已注册的资源读得到。"""
    from app.repo.skill_repo import skill_repo

    sk = await skill_repo.create(
        db, code="demo", name="demo", description="d", body="b", content_hash="h",
    )
    await skill_repo.put_resource(db, sk.id, "references/M.md", "指标口径".encode(), "text/markdown", 1)

    ok, text = await read_skill_resource(db, "demo", "references/M.md", {"demo"}, 4000)
    assert ok is True
    assert "指标口径" in text


async def test_read_unknown_path_returns_not_found(db):
    """查不到的路径只说没有，不泄露任何东西。"""
    from app.repo.skill_repo import skill_repo

    await skill_repo.create(
        db, code="demo", name="demo", description="d", body="b", content_hash="h",
    )
    ok, text = await read_skill_resource(db, "demo", "../../etc/passwd", {"demo"}, 4000)
    assert ok is False
    assert "etc/passwd" not in text


async def test_read_unbound_skill_rejected(db):
    """没绑定的技能读不到。"""
    from app.repo.skill_repo import skill_repo

    await skill_repo.create(
        db, code="demo", name="demo", description="d", body="b", content_hash="h",
    )
    ok, text = await read_skill_resource(db, "demo", "references/M.md", {"other"}, 4000)
    assert ok is False


async def test_binary_resource_returns_metadata_only(db):
    """二进制资源不把字节塞进上下文。"""
    from app.repo.skill_repo import skill_repo

    sk = await skill_repo.create(
        db, code="demo", name="demo", description="d", body="b", content_hash="h",
    )
    await skill_repo.put_resource(db, sk.id, "assets/t.xlsx", b"\x00\x01\x02", "application/vnd.ms-excel", 0)

    ok, text = await read_skill_resource(db, "demo", "assets/t.xlsx", {"demo"}, 4000)
    assert ok is True
    assert "t.xlsx" in text
    assert "\x00" not in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_skill_resource_tool.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.core.skill_reader'`

- [ ] **Step 3: 写 `app/core/skill_reader.py`**

```python
"""读技能资源（设计文档 §22.7）。

路径是**查表的键**，不是路径解析的结果——所以这里没有 ``resolve()``、没有
``is_relative_to``、没有符号链接。``../../etc/passwd`` 查不到行，就什么也读不到。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.skill_paths import SkillPathError, validate_resource_path


async def read_skill_resource(
    db: AsyncSession,
    code: str,
    path: str,
    allowed_codes: set[str] | None,
    max_chars: int,
) -> tuple[bool, str]:
    """读一个技能资源。

    Args:
        db: 数据库会话。
        code: 技能 code。
        path: 资源相对路径。
        allowed_codes: 该 Agent 绑定的技能 code 集合；``None`` 表示不限制。
        max_chars: 返回文本的字符上限。

    Returns:
        ``(ok, text)``。``ok`` 为 False 时 ``text`` 是给模型看的错误说明。
    """
    from app.repo.skill_repo import skill_repo

    if allowed_codes is not None and code not in allowed_codes:
        return False, f"技能「{code}」不在本 Agent 的授权列表里，不能读取。"
    try:
        cleaned = validate_resource_path(path)
    except SkillPathError as exc:
        return False, f"错误：{exc}"

    skill = await skill_repo.get_by_code(db, code)
    if skill is None:
        return False, f"技能「{code}」不存在。"

    row = await skill_repo.get_resource(db, skill.id, cleaned)
    if row is None:
        return False, f"技能「{code}」里没有资源「{cleaned}」。"

    if not row.is_text:
        return True, (
            f"资源「{cleaned}」是二进制文件（{row.mime_type}，{row.size_bytes} 字节）。"
            "本通道只传文本，无法直接读取它的内容。"
        )
    text = row.content.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"…（内容过长，已截断到 {max_chars} 字符）"
    return True, text
```

- [ ] **Step 4: 注册成内置工具**

`app/core/tools.py` 里加一条 `ToolSpec`，名字 `skill_resource`：

- `description`：说明「读取技能的资源文件（references/、assets/）。需要细节时先读它。」
- `parameters`：两个字符串属性 `skill`（必填）与 `path`（必填），`additionalProperties: False`。
- `handler`：一个**异步**函数，签名 `(args: dict) -> str`，内部开自己的 `AsyncSession`（用 `app.config.db.SessionLocal`），调 `read_skill_resource`，把 `(ok, text)` 折成返回文本。

注意 handler 的约定：`app/core/tools.py` 现有 handler 返回 `str`（失败时抛 `ToolCallError`，由 `execute_tool` 统一转成 `(False, 说明)`）。所以 `skill_resource` 的 handler **应当抛 `ToolCallError`** 而不是返回 `(ok, text)`：

```python
async def _skill_resource(args: dict) -> str:
    """读取技能的资源文件。"""
    from app.config.db import SessionLocal

    code = args.get("skill")
    path = args.get("path")
    if not isinstance(code, str) or not code.strip():
        raise ToolCallError("缺少 skill 参数")
    if not isinstance(path, str) or not path.strip():
        raise ToolCallError("缺少 path 参数")
    async with SessionLocal() as db:
        ok, text = await read_skill_resource(
            db, code.strip(), path, allowed_codes=_allowed_skill_codes(), max_chars=settings.tool_result_max_chars
        )
    if not ok:
        raise ToolCallError(text)
    return text
```

**但是**：内置工具的 handler 没有 Agent 上下文，拿不到「本 Agent 绑了哪些技能」。两个选择：
(a) 允许读任意已存在的技能（白名单只在这一层由 `skill_resource` 的参数表达）——**不安全**；
(b) 引入一个**上下文变量**（`contextvars`）保存当前节点的 Agent 绑定技能，handler 从里读。

**选 (b)**。在 `app/core/skill_context.py` 里加：

```python
# 当前执行上下文里「这个 Agent 绑了哪些技能」。内置工具没有 Agent 参数，
# 只能从这里取——用 contextvars 是因为一次 run 里可能有多个节点并发跑。
_current_skill_codes: ContextVar[set[str] | None] = ContextVar(
    "current_skill_codes", default=None
)


def set_current_skill_codes(codes: set[str] | None) -> Token:
    """设置当前上下文的技能白名单，返回 Token 供复位。"""
    return _current_skill_codes.set(codes)


def reset_current_skill_codes(token: Token) -> None:
    """复位技能白名单。"""
    _current_skill_codes.reset(token)


def current_skill_codes() -> set[str] | None:
    """当前上下文绑定的技能 code；``None`` 表示不限制。"""
    return _current_skill_codes.get()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_skill_resource_tool.py -v`
Expected: PASS

- [ ] **Step 6: 跑全量回归**

Run: `pytest -q`
Expected: 全部通过（新增前 166 个 + 本批新增）

- [ ] **Step 7: 提交**

```bash
git add app/core/skill_reader.py app/core/skill_context.py app/core/tools.py tests/test_skill_resource_tool.py
git commit -m "feat(skill): skill_resource 工具"
```

---

### Task 8: 编排器注入

**Files:**
- Modify: `app/core/orchestrator.py`
- Modify: `app/config/settings.py`
- Test: `tests/test_skill_injection.py`

**Interfaces:**
- Consumes: Task 4 的 `SkillPrompt` / `build_skill_messages`；Task 7 的 `set_current_skill_codes` / `reset_current_skill_codes`。
- Produces: `Orchestrator._build_skill_messages(agent) -> list[dict]`。

- [ ] **Step 1: 加配置项**

`app/config/settings.py`：

```python
    # ---- 技能（V4，§22.6）----
    # 技能正文进提示词的总字符预算。超出预算的技能**整个不注入**（宁少勿半）。
    skill_prompt_budget: int = 4000
    # 一次最多注入几个技能。
    skill_max_count: int = 5
```

- [ ] **Step 2: 写失败的测试**

`tests/test_skill_injection.py`：断言**发给 LLM 的 messages** 里技能正文的位置与顺序。

（复用 `tests/test_tool_e2e.py` 里「起真实 uvicorn + 假模型、断言第二轮请求体」的做法——先读那个文件，照抄它的 fixture 与断言方式。核心断言：请求体 `messages[0]` 是 Agent 的 system_prompt，`messages[1]` 是技能正文，`messages[-1]` 是 user 输入。）

- [ ] **Step 3: 改编排器**

`app/core/orchestrator.py`：

1. 加一个私有方法：

```python
    async def _build_skill_messages(self, agent) -> list[dict]:
        """读出该 Agent 绑定的技能正文，拼成 system 消息。

        技能正文取的是**执行时刻**数据库里的内容——技能改了，下一次节点执行就用
        新的版本。已在跑的 run 不受影响（它每次进节点都重新读）。
        """
        codes = known_skill_codes(agent.skills)
        if not codes:
            return []
        async with SessionLocal() as db:
            rows = await skill_repo.list_by_codes(db, codes)
        prompts = [
            SkillPrompt(code=s.code, name=s.name, version=s.version, body=s.body)
            for s in rows
            if s.enabled
        ]
        plan = build_skill_messages(
            prompts, budget=settings.skill_prompt_budget, max_count=settings.skill_max_count
        )
        for code, reason in plan.skipped:
            logger.warning("技能 %s 未注入：%s", code, reason)
        return plan.messages
```

（`skill_repo.list_by_codes` 是 Task 3 之外要补的一个方法——按 code 集合取技能，**按传入顺序返回**。在 Task 3 里一并实现。）

2. 在 `_run_node_with_retry` 里，**重试循环之前**取一次技能消息（取一次就够，重试时复用同一份——重试是为了让同一个节点再试一次，不是让它看到不同的技能版本）：

```python
        skill_messages = await self._build_skill_messages(agent)
        token = set_current_skill_codes(set(known_skill_codes(agent.skills) or []))
        try:
            for attempt in range(1, attempts + 1):
                ...
                messages = (
                    [{"role": MessageRole.SYSTEM.value, "content": agent.system_prompt}]
                    + skill_messages
                    + context
                    + [{"role": MessageRole.USER.value, "content": rendered_input}]
                )
                ...
        finally:
            reset_current_skill_codes(token)
```

注意 `finally` 必须复位 contextvar——否则下一次节点会继承上一次的技能白名单。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_skill_injection.py -v`
Expected: PASS

- [ ] **Step 5: 跑全量回归**

Run: `pytest -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/core/orchestrator.py app/config/settings.py tests/test_skill_injection.py
git commit -m "feat(skill): 编排器注入技能正文"
```

---

### Task 9: 导入 / 导出 / 回滚

**Files:**
- Create: `app/core/skill_import.py`
- Test: `tests/test_skill_import.py`

**Interfaces:**
- Produces:
  - `skill_import.MAX_ZIP_ENTRIES = 2000`、`MAX_ZIP_ENTRY_BYTES = 5 * 1024 * 1024`、`MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024`、`MAX_ZIP_RATIO = 200`
  - `skill_import.extract_zip_safely(data: bytes) -> list[tuple[str, bytes]]` —— 返回 `[(path, content)]`；路径非法、含符号链接、超过上限 → 抛 `SkillImportError`
  - `skill_import.read_zip_safely(path: str) -> list[tuple[str, bytes]]` —— 从磁盘读 zip 再调上面的
  - `skill_import.build_skill_md_zip(skill_md: str, resources: list[tuple[str, bytes]]) -> bytes`

- [ ] **Step 1: 写失败的测试**

```python
"""技能导入的防护（设计文档 §22.10）。"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.core.skill_import import SkillImportError, extract_zip_safely


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_normal_zip_extracted():
    """正常条目原样取出。"""
    got = extract_zip_safely(_zip({"SKILL.md": b"x", "references/a.md": b"y"}))
    assert dict(got) == {"SKILL.md": b"x", "references/a.md": b"y"}


def test_zip_slip_rejected():
    """含 .. 的条目一律拒——这是 zip slip。"""
    with pytest.raises(SkillImportError):
        extract_zip_safely(_zip({"../../etc/passwd": b"x"}))


def test_absolute_path_rejected():
    """绝对路径一律拒。"""
    with pytest.raises(SkillImportError):
        extract_zip_safely(_zip({"/etc/passwd": b"x"}))


def test_symlink_entry_rejected():
    """符号链接条目一律拒。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)  # 符号链接
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(SkillImportError):
        extract_zip_safely(buf.getvalue())


def test_zip_bomb_rejected():
    """解压后总量超限 → 拒。"""
    with pytest.raises(SkillImportError):
        extract_zip_safely(_zip({"big.bin": b"0" * (6 * 1024 * 1024)}))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_skill_import.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.core.skill_import'`

- [ ] **Step 3: 写 `app/core/skill_import.py`**

```python
"""技能包的导入与导出（设计文档 §22.10、§22.11）。

导入是**唯一**一条内容来源不可信的入口（别人写的技能包），所以它有自己的一套
防护。导出则是导入的逆运算——``import(export(x)) == x`` 是不变式。
"""

from __future__ import annotations

import io
import zipfile

from app.core.skill_paths import SkillPathError, validate_resource_path

# zip 的三道上限。entry 数、单文件大小、解压后总量——防 zip bomb。
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_ENTRY_BYTES = 5 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024
# 压缩比上限：正常文本 zip 压缩比也就几十倍，200 足够宽松。
MAX_ZIP_RATIO = 200

# SKILL.md 在包里的固定名字。
SKILL_MD_NAME = "SKILL.md"


class SkillImportError(Exception):
    """导入失败——包结构不合法或超出上限。"""


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """判断 zip entry 是不是符号链接。

    判据是 unix 文件模式的高位：``stat.S_ISLNK(mode)``。符号链接 entry 能让解压
    出来的文件指向包外的路径——那是 zip slip 的另一种形态。
    """
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def extract_zip_safely(data: bytes) -> list[tuple[str, bytes]]:
    """安全地解开一个技能包 zip。

    Args:
        data: zip 字节。

    Returns:
        ``[(path, content), ...]``。

    Raises:
        SkillImportError: 不是合法 zip、条目数超限、含符号链接、路径非法、
            单文件超限或解压后总量超限。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillImportError(f"不是合法的 zip：{exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise SkillImportError(f"zip 内条目数超过 {MAX_ZIP_ENTRIES}")
        compressed = sum(i.compress_size for i in infos) or 1
        out: list[tuple[str, bytes]] = []
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            if _is_symlink(info):
                raise SkillImportError(f"zip 内含符号链接条目：{info.filename}")
            name = info.filename
            if name.startswith("/") or ".." in name.split("/"):
                raise SkillImportError(f"zip 内含非法路径：{name}")
            if name.startswith("./"):
                name = name[2:]
            try:
                validate_resource_path(name)
            except SkillPathError as exc:
                raise SkillImportError(f"zip 内含非法路径：{name}（{exc}）") from exc
            if info.file_size > MAX_ZIP_ENTRY_BYTES:
                raise SkillImportError(f"zip 内单文件超过 {MAX_ZIP_ENTRY_BYTES} 字节：{name}")
            content = zf.read(info)
            total += len(content)
            if total > MAX_ZIP_TOTAL_BYTES:
                raise SkillImportError(f"zip 解压后总量超过 {MAX_ZIP_TOTAL_BYTES} 字节")
            out.append((name, content))
        if total / compressed > MAX_ZIP_RATIO:
            raise SkillImportError(f"zip 压缩比超过 {MAX_ZIP_RATIO}")
        return out


def build_skill_md_zip(skill_md: str, resources: list[tuple[str, bytes]]) -> bytes:
    """把 SKILL.md 与资源打成 zip（导出的逆运算）。

    Args:
        skill_md: SKILL.md 原文。
        resources: ``[(path, content), ...]``。

    Returns:
        zip 字节。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(SKILL_MD_NAME, skill_md)
        for path, content in resources:
            zf.writestr(path, content)
    return buf.getvalue()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_skill_import.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/core/skill_import.py tests/test_skill_import.py
git commit -m "feat(skill): 技能包导入导出与 zip 防护"
```

---

### Task 10: Skill API 路由

**Files:**
- Create: `app/schemas/skill.py`
- Create: `app/api/routes/skill.py`
- Modify: `app/api/router.py`
- Test: `tests/test_skill_api.py`

**Interfaces:**
- Consumes: Task 3 的 `skill_repo`、Task 5 的 `AdminDep`、Task 9 的导入导出函数。
- Produces: 路由 `/api/skills`（只读 + 写入两套，见 §22.11 的表）。

- [ ] **Step 1: 写 schema**

`app/schemas/skill.py`：`SkillOut`、`SkillCreate`、`SkillUpdate`、`SkillResourceOut`、`SkillRevisionOut`、`SkillImportIn`。

`SkillCreate` 必填 `code`（`^[a-z0-9]+(-[a-z0-9]+)*$`，≤64）与 `description`（≤1024）；`body` 默认空串；`enabled` 默认 True。

`SkillUpdate` 全部可选（`exclude_unset` 语义）。

`SkillOut` 用 `ORMBase`（`from_attributes=True`），字段与 `Skill` 表对应，`metadata` 用 `validation_alias="metadata"` 从 ORM 的 `metadata_` 取——**注意 ORM 上这列叫 `metadata_`（`metadata` 是 SQLAlchemy 的保留名）**，用 `Field(validation_alias="metadata")` + `alias` 处理。

- [ ] **Step 2: 写路由**

`app/api/routes/skill.py`，`router = APIRouter(prefix="/api/skills", tags=["skills"])`。

**只读**（无 admin 依赖）：
- `GET ""` → 列表
- `GET "/{code}"` → 详情（不存在 404）
- `GET "/{code}/resources"` → 资源清单
- `GET "/{code}/resources/{path:path}"` → 单个资源（`path` 用 `:path` 转换器才能带斜杠）
- `GET "/{code}/export"` → 返回 zip（`Response(content=..., media_type="application/zip")`）
- `GET "/{code}/revisions"` → 变更历史
- `GET "/{code}/rollback"` **不在这套里**——回滚是写操作，放写入侧。

**写入**（`AdminDep` 依赖）：
- `POST ""` → 创建（201），写 `skill_revision`（version=1）
- `PUT "/{code}"` → 更新，`version+1`，写 revision
- `DELETE "/{code}"` → 删（204）
- `POST "/{code}/resources"` → 上传/替换资源（body 用 `bytes`，带 `X-Resource-Path` 头或 query 参数给路径）
- `DELETE "/{code}/resources/{path:path}"` → 删资源
- `POST "/import"` → 导入 zip（`multipart`），`origin='import'`，**`enabled` 默认 False**
- `POST "/{code}/rollback"` → 回滚到指定版本（body 里给 `version`）

每个写接口都要：
- 重新算 `content_hash`（正文 + 资源）；
- 写一条 `skill_revision`（author 来自 `AdminDep`）；
- 写接口**不**回显明文凭据类的东西（本功能没有凭据，但导入的 zip 里可能有——不要把导入包的原始字节回显）。

- [ ] **Step 3: 注册路由**

`app/api/router.py`：加 `from app.api.routes import ..., skill` 与 `api_router.include_router(skill.router)`。

- [ ] **Step 4: 写失败的测试**

`tests/test_skill_api.py`：

```python
"""Skill API（设计文档 §22.11）。"""

from __future__ import annotations

import pytest


async def test_create_requires_admin(client):
    """没带 admin key → 401（fail-closed）。"""
    resp = await client.post(
        "/api/skills",
        json={"code": "demo", "description": "演示", "body": "正文"},
    )
    assert resp.status_code == 401


async def test_create_and_read(client, admin_headers):
    """管理员建了能读回来。"""
    resp = await client.post(
        "/api/skills",
        json={"code": "demo", "description": "演示", "body": "正文"},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    got = await client.get("/api/skills/demo")
    assert got.status_code == 200
    assert got.json()["body"] == "正文"


async def test_import_defaults_disabled(client, admin_headers, tmp_path):
    """导入的技能默认 disabled——来源不可信。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: imported\ndescription: d\n---\n正文\n")
    resp = await client.post(
        "/api/skills/import",
        files={"file": ("pkg.zip", buf.getvalue(), "application/zip")},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["enabled"] is False
    assert resp.json()["origin"] == "import"
```

（`client` / `admin_headers` fixture 的写法**先读 `tests/test_api_e2e.py`**，照抄它的 ASGI client 构造方式；`admin_headers` 就是要带 `X-Admin-Key` 的头，且 `conftest.py` 里要设好 `ADMIN_KEY` 环境变量——**环境变量必须在 import `app.*` 之前设定**，见 `tests/conftest.py` 顶部的说明。）

- [ ] **Step 5: 跑测试确认通过**

Run: `pytest tests/test_skill_api.py -v`
Expected: PASS

- [ ] **Step 6: 跑全量回归**

Run: `pytest -q`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add app/schemas/skill.py app/api/routes/skill.py app/api/router.py tests/conftest.py tests/test_skill_api.py
git commit -m "feat(skill): 技能 API（CRUD / 导入导出 / 回滚）"
```

---

## 自查清单

- [ ] 设计文档 §22.4（全部进 DB、SKILL.md 降为导出格式）→ Task 2、Task 3、Task 9、Task 10
- [ ] §22.5（三张表 + agent.skills + content_hash）→ Task 1、Task 3、Task 6
- [ ] §22.6（注入形态、预算、宁少勿半）→ Task 4、Task 8
- [ ] §22.7（路径格式校验、查表而非解析、二进制只回元信息）→ Task 2、Task 7
- [ ] §22.8（skill_exec → studio）→ **Phase 3**，本轮不做
- [ ] §22.9（allowed-tools 仅建议）→ Task 1（字段原样存）、Task 6（不参与授权）
- [ ] §22.10（admin 门禁、审计、导入默认禁用、zip 三道防护）→ Task 5、Task 9、Task 10
- [ ] §22.11（只读 / 写入两套 API）→ Task 10
- [ ] §22.12（已知限制）→ 本计划的取舍已在对应任务里写明
- [ ] §22.13（验收标准）→ 各任务的测试用例逐条对应

**已知不在本计划内**：MCP 客户端（Phase 2）、studio 服务与 `skill_exec`（Phase 3）、多租户隔离（设计文档 §22.12 第 9 条，本项目无租户概念）、草稿态与软删除（§22.12 第 11、12 条）。
