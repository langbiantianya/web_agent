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

    # 先看授权再看存在性：不该读的技能，连「它在不在」都不该告诉模型。
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
        # 二进制资源不把字节塞进上下文——模型读不懂一坨 zip，反而会把它当成
        # 文本乱解读。只给元信息，让模型知道「有这个文件、内容拿不到」。
        return True, (
            f"资源「{cleaned}」是二进制文件（{row.mime_type}，{row.size_bytes} 字节）。"
            "本通道只传文本，无法直接读取它的内容。"
        )
    text = row.content.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"…（内容过长，已截断到 {max_chars} 字符）"
    return True, text
