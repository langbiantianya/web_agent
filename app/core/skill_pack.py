"""SKILL.md 的解析 / 拼装 / 指纹（设计文档 §22.4、§22.5）。

``SKILL.md`` 是 Agent Skills 开放格式里的**导出格式**：frontmatter + Markdown 正文。
本项目把它拆成「DB 的若干列 + 一段 body」，于是需要一对互逆的函数：

- :func:`parse_skill_md` —— 导出物 → 结构化字段（导入时用）；
- :func:`render_skill_md` —— 结构化字段 → 导出物（导出时用）。

两者**必须互逆**（``test_render_roundtrip`` 钉住这件事）：导出后能原样导回去，
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

# 领域层的硬上限（V4，§22.10）。
#
# - 正文 20000 字符：正文会整段进提示词，超了就是白烧 token；
# - 单资源 5 MB：一个技能包里放张图或一个数据集就到这个量级；
# - 单技能资源总量 20 MB：防止一个技能把库的存储吃光。
#
# 注意这些是**领域上限**，与传输层无关：HTTP 上传还要过请求体上限中间件
# （见 ``settings.skill_upload_max_bytes``），那一条更严。
MAX_BODY_CHARS = 20_000
MAX_RESOURCE_BYTES = 5 * 1024 * 1024
MAX_SKILL_RESOURCE_BYTES = 20 * 1024 * 1024

# 自己维护的「扩展名 → mime / 是不是文本」表。
#
# 刻意不只靠 ``mimetypes``：那张表随平台而变（Windows 会把注册表里的映射合进来），
# 而「这个资源是不是文本」决定它能不能进提示词、能不能参与回滚——这个判断必须在
# 任何机器上给同一个答案。
_TEXT_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".rst": "text/x-rst",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".ts": "text/plain",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".sql": "application/sql",
    ".env": "text/plain",
}


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

    按**行**切，而不是按 ``"\\n---"`` 切：后者会把正文里的 ``----``（四级连字符）
    也当成闭合标记，而且 CRLF 文件会切出带 ``\\r`` 的行。

    Args:
        text: SKILL.md 原文。

    Returns:
        ``(frontmatter_dict, body)``。

    Raises:
        SkillPackError: 没有 frontmatter，或 YAML 解析失败。
    """
    if not isinstance(text, str):
        raise SkillPackError("SKILL.md 内容必须是字符串")
    # 统一换行：Windows 上编辑过的文件是 CRLF。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillPackError("SKILL.md 缺少 YAML frontmatter")
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise SkillPackError("SKILL.md 的 frontmatter 没有闭合")
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
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

    用 ``yaml.safe_dump`` 而不是手拼 ``key: value``：描述里带 ``:`` 或 ``#`` 时手拼
    会产出**解析不回去**的 YAML（``description: 注意: 这里`` 会被截断）。

    Args:
        fm: frontmatter 字段。
        body: Markdown 正文。

    Returns:
        SKILL.md 原文（以换行结尾）。
    """
    data: dict[str, object] = {"name": fm.name, "description": fm.description}
    if fm.license:
        data["license"] = fm.license
    if fm.compatibility:
        data["compatibility"] = fm.compatibility
    if fm.metadata:
        data["metadata"] = dict(fm.metadata)
    if fm.allowed_tools:
        data["allowed-tools"] = fm.allowed_tools
    front = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{front}---\n\n{body.rstrip()}\n"


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


def _extension(path: str) -> str:
    """取小写扩展名（含点）；没有扩展名返回空串。"""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def guess_mime_type(path: str) -> str:
    """按扩展名猜 mime 类型；猜不到就给二进制。"""
    ext = _extension(path)
    if ext in _TEXT_MIME:
        return _TEXT_MIME[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def is_text_path(path: str) -> bool:
    """判断这个路径下的文件是不是文本。

    决定它能不能进提示词、能不能参与回滚（§22.12 第 7 条）。
    """
    ext = _extension(path)
    if ext in _TEXT_MIME:
        return True
    mime = guess_mime_type(path)
    return mime.startswith("text/")
