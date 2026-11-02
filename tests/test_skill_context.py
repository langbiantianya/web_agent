"""技能正文注入提示词（设计文档 §22.6）。"""

from __future__ import annotations

from app.core.skill_context import SkillPrompt, build_skill_messages


def _skill(code: str, body: str) -> SkillPrompt:
    """造一条待注入的技能。"""
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


def test_order_is_preserved():
    """注入顺序 = 声明顺序——运营靠顺序表达优先级。"""
    skills = [_skill("a", "AAA"), _skill("b", "BBB")]
    plan = build_skill_messages(skills, budget=100000, max_count=5)
    assert "AAA" in plan.messages[0]["content"]
    assert "BBB" in plan.messages[1]["content"]


def test_skipped_records_reason():
    """跳过的原因要写清楚——运维得知道为什么技能没生效。"""
    plan = build_skill_messages([_skill("a", "x" * 100)], budget=10, max_count=5)
    assert plan.skipped
    code, reason = plan.skipped[0]
    assert code == "a"
    assert "预算" in reason
