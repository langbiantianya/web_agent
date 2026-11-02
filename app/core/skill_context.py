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

from contextvars import ContextVar, Token
from dataclasses import dataclass, field


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
    # **实际注入了**的技能 code。工具侧的技能白名单要用它——被预算挡掉的技能，
    # 模型压根没收到它的指令，自然也不该读到它的资源。
    codes: list[str] = field(default_factory=list)


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
    codes: list[str] = []
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
        codes.append(skill.code)
    return SkillPromptPlan(messages=messages, skipped=skipped, codes=codes)


# ---------------------------------------------------------------- 上下文变量

# 当前执行上下文里「这个 Agent 绑了哪些技能」。内置工具没有 Agent 参数，
# 只能从这里取——用 contextvars 是因为一次 run 里可能有多个节点并发跑，
# 而普通模块级变量在并发下会互相踩。
_current_skill_codes: ContextVar[set[str] | None] = ContextVar(
    "current_skill_codes", default=None
)


def set_current_skill_codes(codes: set[str] | None) -> Token:
    """设置当前上下文的技能白名单，返回 Token 供复位。

    Args:
        codes: 允许读取的技能 code 集合；``None`` 表示不限制。

    Returns:
        复位用的 Token。
    """
    return _current_skill_codes.set(codes)


def reset_current_skill_codes(token: Token) -> None:
    """复位技能白名单。

    Args:
        token: :func:`set_current_skill_codes` 返回的 Token。
    """
    _current_skill_codes.reset(token)


def current_skill_codes() -> set[str] | None:
    """当前上下文绑定的技能 code；``None`` 表示不限制。"""
    return _current_skill_codes.get()
