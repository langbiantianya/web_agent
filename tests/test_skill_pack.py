"""技能路径校验与 SKILL.md 解析（设计文档 §22.4、§22.7）。"""

from __future__ import annotations

import pytest

from app.core.skill_pack import (
    SkillPackError,
    compute_content_hash,
    guess_mime_type,
    is_text_path,
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
        "/etc/passwd",  # 绝对路径
        "../../etc/passwd",  # 上跳
        "a/../b",  # 中间上跳
        "a//b",  # 空段
        "a\\b",  # Windows 分隔符
        "a b.md",  # 空格
        "a/b.md/",  # 结尾斜杠 → 空段
        "x" * 256,  # 超长
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


def test_path_strips_surrounding_whitespace():
    """首尾空白剥掉——手写路径时很容易带上。"""
    assert validate_resource_path("  references/a.md  ") == "references/a.md"


def test_non_string_path_rejected():
    """不是字符串就拒，而不是让它在后面炸成别的错。"""
    with pytest.raises(SkillPathError):
        validate_resource_path(None)  # type: ignore[arg-type]


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


def test_parse_skill_md_unclosed_frontmatter():
    """frontmatter 没闭合 → 报错。"""
    with pytest.raises(SkillPackError):
        parse_skill_md("---\nname: x\ndescription: d\n")


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


def test_parse_skill_md_rejects_bad_name_shape():
    """name 以连字符开头/结尾、连续连字符 → 报错（规范原文）。"""
    for bad in ("-lead", "trail-", "double--dash"):
        with pytest.raises(SkillPackError):
            parse_skill_md(f"---\nname: {bad}\ndescription: d\n---\n正文\n")


def test_parse_skill_md_accepts_allowed_tools():
    """allowed-tools 读进字段（只是建议，不用于授权）。"""
    parsed = parse_skill_md(
        "---\nname: x\ndescription: d\nallowed-tools: calc, now\n---\n正文\n"
    )
    assert parsed.frontmatter.allowed_tools == "calc, now"


def test_parse_skill_md_accepts_runtime_metadata():
    """运行档用扁平键声明，导入期能读出来。"""
    parsed = parse_skill_md(
        "---\nname: x\ndescription: d\nmetadata:\n"
        "  runtime-profile: isolated\n  runtime-image: ml\n"
        "  runtime-timeout-ms: '60000'\n---\n正文\n"
    )
    assert parsed.frontmatter.metadata["runtime-profile"] == "isolated"
    assert parsed.frontmatter.metadata["runtime-image"] == "ml"


def test_parse_skill_md_rejects_bad_runtime_metadata():
    """非法运行档声明在导入期就拒，不留到执行时。"""
    with pytest.raises(SkillPackError):
        parse_skill_md(
            "---\nname: x\ndescription: d\nmetadata:\n"
            "  runtime-profile: gpu\n---\n正文\n"
        )


def test_parse_skill_md_handles_crlf():
    """Windows 上编辑过的 SKILL.md 有 CRLF，解析不能因此失败。"""
    text = "---\r\nname: x\r\ndescription: d\r\n---\r\n\r\n正文\r\n"
    parsed = parse_skill_md(text)
    assert parsed.frontmatter.name == "x"
    assert parsed.body.strip() == "正文"


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


def test_render_roundtrip_with_special_characters():
    """描述里带冒号也要能往返——YAML 的 ``key: value`` 会被冒号截断。"""
    text = (
        "---\n"
        "name: demo\n"
        "description: '注意: 这里有个冒号，还有 # 号'\n"
        "metadata:\n"
        "  version: '1.0'\n"
        "---\n"
        "\n"
        "正文。\n"
    )
    parsed = parse_skill_md(text)
    again = parse_skill_md(render_skill_md(parsed.frontmatter, parsed.body))
    assert again.frontmatter.description == parsed.frontmatter.description
    assert again.frontmatter.metadata == parsed.frontmatter.metadata


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


def test_content_hash_changes_when_resource_renamed():
    """资源改名也算内容变了——脚本的相对路径引用会跟着断。"""
    a = compute_content_hash("body", [("a.md", b"x")])
    b = compute_content_hash("body", [("b.md", b"x")])
    assert a != b


# ------------------------------------------------------------- mime 判定


def test_guess_mime_type():
    """按扩展名猜；猜不到给二进制。"""
    assert guess_mime_type("a.md").startswith("text/")
    assert guess_mime_type("a.unknownext") == "application/octet-stream"


def test_is_text_path():
    """文本资源才进提示词、才参与回滚。"""
    assert is_text_path("references/M.md") is True
    assert is_text_path("data.json") is True
    assert is_text_path("assets/t.xlsx") is False
    assert is_text_path("assets/t.png") is False
