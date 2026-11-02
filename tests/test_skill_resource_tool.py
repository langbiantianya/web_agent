"""skill_resource 工具（设计文档 §22.7）。"""

from __future__ import annotations

import itertools

from app.core.skill_reader import read_skill_resource
from app.repo.skill_repo import skill_repo

_counter = itertools.count()


async def _make_skill(db, code: str | None = None):
    """建一个技能，返回它。"""
    code = code or f"sk_{next(_counter)}"
    return await skill_repo.create(
        db, code=code, name=code, description="d", body="b", content_hash="h"
    )


async def test_read_registered_resource(db):
    """已注册的资源读得到。"""
    sk = await _make_skill(db)
    await skill_repo.put_resource(
        db, sk.id, "references/M.md", "指标口径".encode(), "text/markdown", 1
    )

    ok, text = await read_skill_resource(db, sk.code, "references/M.md", {sk.code}, 4000)
    assert ok is True
    assert "指标口径" in text


async def test_read_unknown_path_returns_not_found(db):
    """查不到的路径只说没有，不泄露任何东西。"""
    sk = await _make_skill(db)
    ok, text = await read_skill_resource(db, sk.code, "../../etc/passwd", {sk.code}, 4000)
    assert ok is False
    assert "etc/passwd" not in text


async def test_read_unbound_skill_rejected(db):
    """没绑定的技能读不到。"""
    sk = await _make_skill(db)
    ok, text = await read_skill_resource(db, sk.code, "references/M.md", {"other"}, 4000)
    assert ok is False


async def test_read_unbound_skill_does_not_leak_existence(db):
    """没绑定的技能，回复不能因为「它存不存在」而不同。

    模型是自己给出这个 code 的，所以回显 code 没问题；有问题的是「存在」与
    「不存在」两种情况给出不同的话——那等于一个探测接口。
    """
    sk = await _make_skill(db)
    ok_exists, text_exists = await read_skill_resource(
        db, sk.code, "references/M.md", {"other"}, 4000
    )
    ok_missing, text_missing = await read_skill_resource(
        db, "definitely_not_here", "references/M.md", {"other"}, 4000
    )
    assert ok_exists is False
    assert ok_missing is False
    assert text_exists.replace(sk.code, "X") == text_missing.replace(
        "definitely_not_here", "X"
    )


async def test_binary_resource_returns_metadata_only(db):
    """二进制资源不把字节塞进上下文。"""
    sk = await _make_skill(db)
    await skill_repo.put_resource(
        db, sk.id, "assets/t.xlsx", b"\x00\x01\x02", "application/vnd.ms-excel", 0
    )

    ok, text = await read_skill_resource(db, sk.code, "assets/t.xlsx", {sk.code}, 4000)
    assert ok is True
    assert "t.xlsx" in text
    assert "\x00" not in text


async def test_long_resource_is_truncated(db):
    """超长资源截断并说明截断了。"""
    sk = await _make_skill(db)
    await skill_repo.put_resource(
        db, sk.id, "references/big.md", b"x" * 500, "text/markdown", 1
    )
    ok, text = await read_skill_resource(db, sk.code, "references/big.md", {sk.code}, 100)
    assert ok is True
    assert "截断" in text
    assert len(text) < 200


async def test_missing_skill_reports_not_found(db):
    """技能不存在 → 明确说不存在。"""
    ok, text = await read_skill_resource(db, "nope", "a.md", None, 4000)
    assert ok is False
    assert "不存在" in text


async def test_allowed_codes_none_means_unrestricted(db):
    """白名单为 None 表示不限制。"""
    sk = await _make_skill(db)
    await skill_repo.put_resource(db, sk.id, "a.md", b"x", "text/markdown", 1)
    ok, _ = await read_skill_resource(db, sk.code, "a.md", None, 4000)
    assert ok is True


# ------------------------------------------------------------- 工具注册


async def test_tool_is_registered():
    """工具在注册表里，schema 与别的内置工具一个样。"""
    from app.core.tools import get_tool

    spec = get_tool("skill_resource")
    assert spec is not None
    assert spec.parameters["required"] == ["skill", "path"]
    assert spec.parameters["additionalProperties"] is False


async def test_execute_tool_honors_context_whitelist(db):
    """通过 execute_tool 调用时，白名单仍然生效。

    工具 handler 拿不到 Agent 参数，白名单只能从 contextvars 来——这条断言
    盯的就是那条链路真的接上了。
    """
    from app.core.skill_context import (
        reset_current_skill_codes,
        set_current_skill_codes,
    )
    from app.core.tools import execute_tool

    sk = await _make_skill(db)
    await skill_repo.put_resource(db, sk.id, "a.md", "内容".encode(), "text/markdown", 1)

    token = set_current_skill_codes({sk.code})
    try:
        ok, text = await execute_tool(
            "skill_resource", {"skill": sk.code, "path": "a.md"}
        )
        assert ok is True
        assert "内容" in text
    finally:
        reset_current_skill_codes(token)

    # 白名单之外：同一个技能读不到
    token = set_current_skill_codes({"other"})
    try:
        ok, text = await execute_tool(
            "skill_resource", {"skill": sk.code, "path": "a.md"}
        )
        assert ok is False
        assert "授权列表" in text
    finally:
        reset_current_skill_codes(token)


async def test_execute_tool_missing_arguments_are_reported(db):
    """参数缺失要说清楚缺哪个，而不是抛一个看不懂的异常。"""
    from app.core.tools import execute_tool

    ok, text = await execute_tool("skill_resource", {"skill": "x"})
    assert ok is False
    assert "path" in text
