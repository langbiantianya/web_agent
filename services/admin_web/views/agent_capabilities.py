"""Agent 的 Skill、内置工具与 MCP 工具选择组件。

这一块对应 UX-19 与 UX-20：

- **UX-19**：MCP 工具一多，所有服务器和工具会一次性铺满整个创建/编辑
  表单——表单能长达几屏，找一个工具要翻很久。这里加了：本地搜索、
  按服务器折叠、已选计数、以及「只看可用」筛选。
- **UX-20**：Skill 的 ``allowed_tools`` 只是原始逗号文本，看不出哪些
  建议的工具**不存在**、**已停用**或**当前 Agent 没勾**。这里把建议
  解析成逐条状态。

## 建议 ≠ 授权

Skill 里的工具建议**只作提示**：不存在的建议给警告，但不自动授权；
真正授权的只有 Agent 上勾选的工具。这条边界在界面上必须看得见，
否则管理员会以为「勾了 Skill 就等于能调它的工具」。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    div,
    fieldset,
    input as input_,
    label,
    legend,
    p,
    small,
    span,
)

from app.core.tools import tool_names


def _choice(
    *,
    name: str,
    value: str,
    title: str,
    detail: str,
    checked: bool,
    unavailable: bool = False,
) -> Any:
    """渲染一个带说明的能力复选框。"""
    return label(
        input_(
            type="checkbox",
            name=name,
            value=value,
            checked=checked,
            disabled=unavailable and not checked,
        ),
        span(title),
        small(detail, class_="block-muted"),
        class_="capability-choice",
        data_capability_value=value,
        data_capability_title=f"{title} {detail}".strip().casefold(),
        data_capability_checked="1" if checked else "",
        data_capability_unavailable="1" if unavailable else "",
    )


def _group(
    title: str,
    choices: list[Any],
    *,
    warning: bool = False,
    subtitle: str = "",
) -> Any:
    """渲染一个带标题的能力分组；空组由调用方跳过。

    标题是一个**折叠开关**（UX-19）：工具多的时候不需要整屏铺开，
    点一下标题就能收起该分组。
    """
    return div(
        div(
            button(
                title,
                type="button",
                class_="capability-group-toggle",
                data_capability_toggle="1",
                aria_expanded="true",
            ),
            *( [small(subtitle, class_="block-muted")] if subtitle else [] ),
            class_="capability-group-header",
        ),
        *choices,
        class_="capability-group is-warning" if warning else "capability-group",
        data_capability_group="1",
    )


def _skill_suggestions(
    skill: Any,
    *,
    known_tools: set[str],
    disabled_tools: set[str],
    chosen_tools: set[str],
) -> list[tuple[str, str, str]]:
    """解析 Skill 的工具建议，返回 ``(工具名, 状态, 说明)`` 列表。

    状态有四种：

    - ``granted``：已勾选，Agent 真的会拿到这个工具；
    - ``not-granted``：工具存在，但当前 Agent 没勾——建议 ≠ 授权；
    - ``disabled``：工具所在服务器已停用；
    - ``missing``：工具在注册表里不存在（服务器换了、工具被删了）。
    """
    raw = str(getattr(skill, "allowed_tools", "") or "")
    names = [item.strip() for item in raw.split(",") if item.strip()]
    result: list[tuple[str, str, str]] = []
    for name in names:
        if name in chosen_tools:
            result.append((name, "granted", "granted to this agent"))
        elif name in disabled_tools:
            result.append((name, "disabled", "server is disabled"))
        elif name not in known_tools:
            result.append((name, "missing", "tool does not exist"))
        else:
            result.append((name, "not-granted", "not granted to this agent"))
    return result


def _suggestion_badges(
    suggestions: list[tuple[str, str, str]],
) -> Any:
    """把建议解析结果渲染成带状态的小标记。"""
    if not suggestions:
        return small("No tool suggestions", class_="block-muted")
    return div(
        span("Suggested tools:", class_="block-muted"),
        *(
            [
                span(
                    f"{name} · {detail}",
                    class_=f"capability-suggestion is-{status}",
                    title=detail,
                    data_capability_suggestion_tool=name,
                    data_capability_suggestion_status=status,
                )
                for name, status, detail in suggestions
            ]
        ),
        class_="capability-suggestions",
        data_capability_suggestions="1",
    )


def capability_selector(
    *,
    skills: list[Any],
    mcp_servers: list[Any],
    mcp_tools: list[Any],
    selected_skills: Iterable[str] = (),
    selected_tools: Iterable[str] = (),
) -> Any:
    """按来源分组渲染 Skill、内置工具与 MCP 工具。"""
    chosen_skills = set(selected_skills)
    chosen_tools = set(selected_tools)
    builtin_names = set(tool_names())
    known_tools = builtin_names | {
        tool.qualified_name for tool in mcp_tools
    }
    disabled_tools = {
        tool.qualified_name
        for tool in mcp_tools
        if not _server_enabled(mcp_servers, tool.server_id)
    }
    missing_tools = sorted(chosen_tools - known_tools)
    missing_skills = sorted(
        chosen_skills - {skill.code for skill in skills}
    )
    skill_choices = [
        _choice(
            name="skills",
            value=skill.code,
            title=f"{skill.name} ({skill.code})",
            detail=(
                f"v{skill.version} · {skill.status}"
                + ("" if skill.enabled else " · disabled")
            ),
            checked=skill.code in chosen_skills,
            unavailable=not skill.enabled or skill.status != "published",
        )
        for skill in sorted(skills, key=lambda item: item.code)
    ]
    # 建议解析结果按 skill code 索引，供下面的说明区使用。
    suggestions_by_skill = {
        skill.code: _skill_suggestions(
            skill,
            known_tools=known_tools,
            disabled_tools=disabled_tools,
            chosen_tools=chosen_tools,
        )
        for skill in skills
    }
    builtin_choices = [
        _choice(
            name="tools",
            value=name,
            title=name,
            detail="Built-in tool",
            checked=name in chosen_tools,
        )
        for name in sorted(builtin_names)
    ]
    mcp_groups = []
    for server in sorted(mcp_servers, key=lambda item: item.code):
        server_tools = [
            tool for tool in mcp_tools if tool.server_id == server.id
        ]
        if not server_tools:
            continue
        mcp_groups.append(
            _group(
                server.name or server.code,
                [
                    _choice(
                        name="tools",
                        value=tool.qualified_name,
                        title=tool.tool_name,
                        detail=(tool.description or tool.qualified_name)[:120],
                        checked=tool.qualified_name in chosen_tools,
                        unavailable=not server.enabled,
                    )
                    for tool in server_tools
                ],
                subtitle=(
                    server.code + ("" if server.enabled else " · disabled")
                ),
            )
        )
    unavailable = [
        _choice(
            name="tools",
            value=name,
            title=name,
            detail="Unavailable binding — uncheck to remove",
            checked=True,
            unavailable=True,
        )
        for name in missing_tools
    ] + [
        _choice(
            name="skills",
            value=code,
            title=code,
            detail="Unavailable binding — uncheck to remove",
            checked=True,
            unavailable=True,
        )
        for code in missing_skills
    ]
    return fieldset(
        legend("Capabilities"),
        p(
            "Bind published Skills and discovered tools. Skill tool hints are "
            "suggestions only; tool access is granted by the checked tools below.",
            class_="block-muted",
        ),
        _capability_toolbar(),
        div(
            *(
                [_group("Published Skills", skill_choices)]
                if skill_choices
                else []
            ),
            _group("Built-in tools", builtin_choices),
            *mcp_groups,
            *(
                [_group("Unavailable bindings", unavailable, warning=True)]
                if unavailable
                else []
            ),
            class_="capability-grid",
        ),
        _skill_suggestion_section(suggestions_by_skill),
        class_="capability-fieldset",
        data_capability_selector="1",
    )


def _server_enabled(mcp_servers: Iterable[Any], server_id: Any) -> bool:
    """判断一台 MCP 服务器是否处于启用状态。"""
    for server in mcp_servers:
        if getattr(server, "id", None) == server_id:
            return bool(getattr(server, "enabled", False))
    return False


def _capability_toolbar() -> Any:
    """渲染能力选择器的工具条：搜索、只看可用、已选计数。"""
    return div(
        label(
            span("Search tools"),
            input_(
                type="search",
                placeholder="Tool name or description",
                autocomplete="off",
                data_capability_search="1",
            ),
            class_="capability-search",
        ),
        label(
            span("Only available"),
            input_(type="checkbox", data_capability_available_only="1"),
            class_="capability-toggle",
        ),
        span(
            "0 selected",
            class_="capability-count",
            data_capability_count="1",
            role="status",
            aria_live="polite",
        ),
        class_="capability-toolbar",
    )


def _skill_suggestion_section(
    suggestions_by_skill: dict[str, list[tuple[str, str, str]]],
) -> Any:
    """渲染「Skill 建议 vs 实际授权」的差异区（UX-20）。"""
    entries = [
        (code, suggestions)
        for code, suggestions in sorted(suggestions_by_skill.items())
        if suggestions
    ]
    if not entries:
        return span("", class_="capability-suggestions-empty", hidden=True)
    return div(
        span("Skill tool suggestions", class_="capability-group-title"),
        *(
            [
                div(
                    span(code, class_="block-muted"),
                    _suggestion_badges(suggestions),
                    class_="capability-suggestion-row",
                )
                for code, suggestions in entries
            ]
        ),
        class_="capability-suggestion-list",
    )


def capability_summary(
    row: Any,
    *,
    skills_by_code: dict[str, Any],
    servers_by_code: dict[str, Any],
    tools_by_name: dict[str, Any],
) -> Any:
    """把 Agent 的 Skill / 工具绑定压缩成列表摘要。"""
    skill_codes = [code for code in row.skills or [] if isinstance(code, str)]
    tool_rows = [name for name in row.tools or [] if isinstance(name, str)]
    missing = (
        [code for code in skill_codes if code not in skills_by_code]
        + [
            name
            for name in tool_rows
            if name not in tools_by_name and name not in tool_names()
        ]
    )
    names = [
        skills_by_code[code].name if code in skills_by_code else code
        for code in skill_codes
    ]
    names += [
        (
            f"{servers_by_code[tools_by_name[name].server_id].code}/"
            f"{tools_by_name[name].tool_name}"
            if name in tools_by_name
            and tools_by_name[name].server_id in servers_by_code
            else name
        )
        for name in tool_rows
    ]
    return div(
        span(f"{len(skill_codes)} skills · {len(tool_rows)} tools"),
        small(
            ", ".join(names) or "No capabilities bound",
            class_="block-muted",
        ),
        *(
            [
                small(
                    f"{len(missing)} unavailable binding(s)",
                    class_="warning-text",
                )
            ]
            if missing
            else []
        ),
    )
