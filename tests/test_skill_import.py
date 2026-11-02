"""技能导入的防护（设计文档 §22.10）。"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.core.skill_import import (
    SKILL_MD_NAME,
    SkillImportError,
    build_skill_md_zip,
    extract_zip_safely,
    parse_skill_package,
)


def _zip(entries: dict[str, bytes]) -> bytes:
    """做一个 zip。"""
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


def test_high_compression_ratio_rejected():
    """压缩比超限 → 在**解压之前**就拒掉。

    十万个相同的零字节压缩后只有几十 KB——不先判压缩比，光是解压就能把内存
    撑爆，那正是 zip bomb 的攻击面。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\x00" * (40 * 1024 * 1024))
    with pytest.raises(SkillImportError):
        extract_zip_safely(buf.getvalue())


def test_not_a_zip_rejected():
    """不是 zip → 明确报错，不是别的什么怪异常。"""
    with pytest.raises(SkillImportError):
        extract_zip_safely(b"this is not a zip")


def test_too_many_entries_rejected():
    """条目数超限 → 拒。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(2001):
            zf.writestr(f"f{i}.md", b"x")
    with pytest.raises(SkillImportError):
        extract_zip_safely(buf.getvalue())


def test_invalid_path_charset_rejected():
    """带空格的路径一律拒——资源路径的字符集白名单不松口。"""
    with pytest.raises(SkillImportError):
        extract_zip_safely(_zip({"references/a b.md": b"x"}))


# ------------------------------------------------------------- 包组装


def test_parse_skill_package():
    """SKILL.md 在根目录，其余是资源。"""
    entries = extract_zip_safely(
        _zip(
            {
                SKILL_MD_NAME: "---\nname: demo\ndescription: 演示\n---\n正文\n".encode(),
                "references/a.md": "引用".encode(),
            }
        )
    )
    package = parse_skill_package(entries)
    assert package.frontmatter.name == "demo"
    assert package.body.strip() == "正文"
    assert package.resources == [("references/a.md", "引用".encode())]


def test_parse_skill_package_requires_skill_md():
    """缺 SKILL.md → 报错。"""
    with pytest.raises(SkillImportError):
        parse_skill_package([("references/a.md", b"x")])


def test_parse_skill_package_rejects_bad_frontmatter():
    """SKILL.md 坏了 → 报错，不猜。"""
    with pytest.raises(SkillImportError):
        parse_skill_package([(SKILL_MD_NAME, "# 没有 frontmatter\n".encode())])


# ------------------------------------------------------------- 导出


def test_build_zip_roundtrip():
    """导出再导入要能回来——导入导出必须对称。"""
    resources = [("references/a.md", "引用".encode()), ("assets/x.bin", b"\x00\x01")]
    data = build_skill_md_zip("---\nname: demo\ndescription: d\n---\n正文\n", resources)
    entries = extract_zip_safely(data)
    assert dict(entries)[SKILL_MD_NAME].startswith(b"---")
    package = parse_skill_package(entries)
    assert package.frontmatter.name == "demo"
    assert package.resources == resources
