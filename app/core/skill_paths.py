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
RESOURCE_PATH_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"

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
        raise SkillPathError("资源路径不能包含 ..")
    if any(seg == "." for seg in segments):
        raise SkillPathError("资源路径不能包含 . 段")
    return cleaned
