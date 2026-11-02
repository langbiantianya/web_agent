"""工具结果的敏感词兜底（§20、§21.10 第 ④ 条、§22.13）。

工具结果会**直接进 prompt**。它来自第三方（MCP 服务器、技能脚本），可能含敏感
内容。§20 的过滤点在「内容进 LLM 之前」——工具结果与用户输入位置一样、性质也
一样，不能只过滤用户输入。

命中时按「拒绝」处理：**不回灌**，一路抛上去让 run 以敏感内容错误收尾。

这里把判定函数换成「必定命中」——敏感词模型本身的行为由
:mod:`tests.test_sensitive` 负责；本文件盯的是**接线**：结果到底有没有被筛。
"""

from __future__ import annotations

import itertools

import pytest

from app.core.errors import SensitiveContentError
from app.core.tools import (
    execute_tool,
    set_optional_tools_enabled,
)
from app.repo.skill_repo import skill_repo

_counter = itertools.count()


class _FakeStudio:
    """假的 studio 客户端。"""

    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """记住脚本输出。"""
        from app.core.studio import ExecResult, StudioHealth

        self.result = ExecResult("completed", exit_code, stdout, stderr, False, 5, "", "d")
        self.health_info = StudioHealth(
            True, "1.0.0", ("python",), 4, "fast"
        )

    async def exec_script(self, **kwargs):
        """假的 exec_script。"""
        return self.result


@pytest.fixture
def always_hit(monkeypatch):
    """把敏感词判定换成「必定命中」。"""
    from app.core import sensitive as sensitive_mod

    monkeypatch.setattr(
        sensitive_mod,
        "screen_text",
        lambda _text: sensitive_mod.SensitiveVerdict(
            hit=True, score=0.99, position=0, excerpt="命中片段"
        ),
    )


@pytest.fixture
def never_hit(monkeypatch):
    """把敏感词判定换成「永不命中」。"""
    from app.core import sensitive as sensitive_mod

    monkeypatch.setattr(
        sensitive_mod,
        "screen_text",
        lambda _text: sensitive_mod.SensitiveVerdict(
            hit=False, score=0.0, position=-1, excerpt=""
        ),
    )


@pytest.fixture
async def skill_with_script(db):
    """建一个带脚本的技能。"""
    code = f"sen_{next(_counter)}"
    sk = await skill_repo.create(
        db, code=code, name=code, description="d", body="b", content_hash="h"
    )
    await skill_repo.put_resource(
        db, sk.id, "scripts/run.py", b"print(1)", "text/x-python", 1
    )
    return sk


@pytest.fixture
def fake_studio(monkeypatch):
    """把模块里的 get_studio_client 换成假客户端。"""
    from app.core import skill_exec as skill_exec_mod

    def _install(studio: _FakeStudio) -> _FakeStudio:
        monkeypatch.setattr(skill_exec_mod, "get_studio_client", lambda: studio)
        return studio

    return _install


@pytest.fixture(autouse=True)
def _registry_on():
    """skill_exec 在这个文件里是「已注册」状态。"""
    set_optional_tools_enabled(True)
    yield
    set_optional_tools_enabled(False)


async def test_sensitive_script_output_is_not_fed_back(
    skill_with_script, fake_studio, always_hit
):
    """脚本 stdout 命中敏感词 → **不回灌**，抛 SensitiveContentError。"""
    fake_studio(_FakeStudio(stdout="这里有一段敏感内容"))
    with pytest.raises(SensitiveContentError):
        await execute_tool(
            "skill_exec",
            {"skill": skill_with_script.code, "script": "scripts/run.py"},
        )


async def test_sensitive_script_stderr_is_not_fed_back(
    skill_with_script, fake_studio, always_hit
):
    """失败的脚本，其 stderr 同样要筛。

    失败分支的文本也会被回灌给模型（模型看到「脚本报错了」有机会自己纠正），
    所以 stderr 里的敏感内容不能因为「这次失败了」就漏过去。
    """
    fake_studio(_FakeStudio(stderr="Traceback: 这里有一段敏感内容", exit_code=1))
    with pytest.raises(SensitiveContentError):
        await execute_tool(
            "skill_exec",
            {"skill": skill_with_script.code, "script": "scripts/run.py"},
        )


async def test_clean_script_output_passes_through(
    skill_with_script, fake_studio, never_hit
):
    """没命中就正常回灌——过滤器不能把正常输出也拦了。"""
    fake_studio(_FakeStudio(stdout="正常输出"))
    ok, text = await execute_tool(
        "skill_exec",
        {"skill": skill_with_script.code, "script": "scripts/run.py"},
    )
    assert ok is True
    assert "正常输出" in text
    assert "exit_code: 0" in text


async def test_mcp_result_uses_the_same_screen(
    fake_studio, always_hit, monkeypatch
):
    """MCP 工具结果走**同一套**筛查。

    两处各自实现一遍的话，迟早有一边漏改——而漏改的那一边就是「敏感内容进了
    prompt」。
    """
    from app.core import tools as tools_mod

    with pytest.raises(SensitiveContentError):
        tools_mod._screen_tool_output("随便什么内容", "mcp__x__y")
