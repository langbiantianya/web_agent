"""技能包的导入与导出（设计文档 §22.10、§22.11）。

导入是**唯一**一条内容来源不可信的入口（别人写的技能包），所以它有自己的一套
防护。导出则是导入的逆运算——``import(export(x)) == x`` 是不变式。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.core.skill_pack import (
    SkillFrontmatter,
    SkillPackError,
    parse_skill_md,
)
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


@dataclass(frozen=True)
class SkillPackage:
    """解开后的技能包。"""

    frontmatter: SkillFrontmatter
    body: str
    # 除 SKILL.md 之外的资源：(path, content)。path 已规整过。
    resources: list[tuple[str, bytes]] = field(default_factory=list)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """判断 zip entry 是不是符号链接。

    判据是 unix 文件模式的高位：``stat.S_ISLNK(mode)``。符号链接 entry 能让解压
    出来的文件指向包外的路径——那是 zip slip 的另一种形态。
    """
    mode = info.external_attr >> 16
    return (mode & 0o170000) == 0o120000


def extract_zip_safely(data: bytes) -> list[tuple[str, bytes]]:
    """安全地解开一个技能包 zip。

    防护顺序是有讲究的：**先看体积，再看内容**。压缩比超限的包在读完之前就被
    拒掉——否则一个几百 KB 的压缩包能撑出几个 GB 的内存，那正是 zip bomb 的
    攻击面。

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

        files = [i for i in infos if not i.is_dir()]
        # 压缩比要在**解压之前**判：解压后再判就晚了，内存已经被撑起来了。
        compressed = sum(i.compress_size for i in files) or 1
        declared = sum(i.file_size for i in files)
        if declared / compressed > MAX_ZIP_RATIO:
            raise SkillImportError(
                f"zip 压缩比超过 {MAX_ZIP_RATIO}（声明解压后 {declared} 字节）"
            )

        out: list[tuple[str, bytes]] = []
        total = 0
        for info in files:
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
                raise SkillImportError(
                    f"zip 内单文件超过 {MAX_ZIP_ENTRY_BYTES} 字节：{name}"
                )
            content = zf.read(info)
            total += len(content)
            if total > MAX_ZIP_TOTAL_BYTES:
                raise SkillImportError(
                    f"zip 解压后总量超过 {MAX_ZIP_TOTAL_BYTES} 字节"
                )
            out.append((name, content))
        return out


def read_zip_safely(path: str) -> list[tuple[str, bytes]]:
    """从磁盘读一个 zip 再解开。

    Args:
        path: zip 文件路径。

    Returns:
        ``[(path, content), ...]``。

    Raises:
        SkillImportError: 文件读不了，或包本身有问题。
    """
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise SkillImportError(f"读不到 zip 文件：{exc}") from exc
    return extract_zip_safely(data)


def parse_skill_package(entries: list[tuple[str, bytes]]) -> SkillPackage:
    """把解开后的条目组装成一个技能包。

    ``SKILL.md`` 必须在**根目录**——它定义了技能本身，其余文件是它的资源。

    Args:
        entries: ``[(path, content), ...]``。

    Returns:
        :class:`SkillPackage`。

    Raises:
        SkillImportError: 缺 SKILL.md、SKILL.md 解析失败。
    """
    skill_md: bytes | None = None
    resources: list[tuple[str, bytes]] = []
    for name, content in entries:
        if name == SKILL_MD_NAME:
            skill_md = content
        else:
            resources.append((name, content))
    if skill_md is None:
        raise SkillImportError(f"包里没有 {SKILL_MD_NAME}")
    try:
        parsed = parse_skill_md(skill_md.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SkillImportError(f"{SKILL_MD_NAME} 不是合法的 UTF-8 文本") from exc
    except SkillPackError as exc:
        raise SkillImportError(f"{SKILL_MD_NAME} 解析失败：{exc}") from exc
    return SkillPackage(
        frontmatter=parsed.frontmatter, body=parsed.body, resources=resources
    )


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
