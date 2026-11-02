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


def known_skill_codes(raw: Any, known: set[str] | None = None) -> list[str] | None:
    """宽容地读出库里存的技能 code——**只读，不报错**。

    与 :func:`normalize_skill_codes` 相反的方向：那个用于**写入**（配置不合法就
    拒绝），这个用于**读取**（历史数据可能脏，但不能让整个列表接口 500）。

    Args:
        raw: 库里存的值。
        known: 已知存在的技能 code 集合。给了就把不在集合里的丢掉——技能被删掉
            之后，Agent 上残留的绑定就该消失，而不是继续显示一个空壳。没给就
            只做形状检查（调用方拿不到库里的全量集合时用）。

    Returns:
        code 列表；形状不对或一个都不认识时返回 ``None``。
    """
    if not isinstance(raw, list):
        return None
    names = [n for n in raw if isinstance(n, str) and n.strip()]
    if known is not None:
        names = [n for n in names if n in known]
    # 去重但保持原有顺序——顺序就是注入提示词时的优先级。
    return list(dict.fromkeys(names)) or None
