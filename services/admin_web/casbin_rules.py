"""Casbin 策略规则的搜索与排序。

规则本身来自 Casbin（不是关系库），因此搜索与排序在内存里做：规则条数
是配置量级，遍历一次的成本可以忽略，而筛选状态仍然放在 URL 上，方便
分享同一个视图（UX-06）。
"""

from __future__ import annotations

from typing import Any


def _rule_text(rule: Any) -> str:
    """把一条 Casbin 规则拼成可搜索的文本。"""
    return " ".join(
        str(getattr(rule, name, "") or "")
        for name in ("ptype", "v0", "v1", "v2", "v3")
    )


def _filter_rules(rules: list[Any], query: str) -> list[Any]:
    """按关键字过滤 Casbin 规则（大小写不敏感）。"""
    keyword = (query or "").strip().casefold()
    if not keyword:
        return list(rules)
    return [
        rule for rule in rules if keyword in _rule_text(rule).casefold()
    ]


def _sort_rules(rules: list[Any], sort: str) -> list[Any]:
    """按白名单字段排序 Casbin 规则。"""
    if sort == "type":
        return sorted(rules, key=lambda rule: str(rule.ptype or ""))
    if sort == "subject":
        return sorted(rules, key=lambda rule: str(rule.v0 or ""))
    return list(rules)
