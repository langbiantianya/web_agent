"""技能脚本执行（设计文档 §22.8.3、§22.8.4）。

要钉住的核心命题：

1. **校验在 web_agent 侧**——studio 没有技能的概念，路径与参数形态只能在这里查。
   校验不过的请求**根本没发出去**（§22.13 的验收项）。
2. **白名单与 skill_resource 同款**——没绑定的技能不能跑它的脚本。
3. **脚本失败不是节点失败**——退出码与 stderr 原样回灌给模型（§17.5）。
4. **web_agent 进程内不起子进程**——执行发生在 studio 那边。
"""

from __future__ import annotations

import itertools

import pytest

from app.core.skill_exec import (
    SkillExecError,
    exec_skill_script,
    format_exec_result,
    guess_language,
    read_script,
    validate_argv,
    validate_script_path,
)
from app.core.studio import ExecResult, StudioHealth, StudioUnavailableError
from app.repo.skill_repo import skill_repo

_counter = itertools.count()


class _FakeStudio:
    """假的 studio 客户端。"""

    def __init__(
        self,
        result: ExecResult | None = None,
        error: Exception | None = None,
        languages: tuple[str, ...] = ("python", "bash", "node"),
    ) -> None:
        """记住要返回什么。"""
        self.result = result or ExecResult("completed", 0, "", "", False, 5, "", "d")
        self.error = error
        self.health_info = StudioHealth(True, "1.0.0", languages, 4, "fast")
        self.calls: list[dict] = []

    async def exec_script(self, **kwargs):
        """假的 exec_script。"""
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


async def _make_skill(db, script: str = "print('hi')"):
    """建一个带脚本的技能。"""
    code = f"exec_{next(_counter)}"
    sk = await skill_repo.create(
        db, code=code, name=code, description="d", body="b", content_hash="h"
    )
    await skill_repo.put_resource(
        db, sk.id, "scripts/run.py", script.encode(), "text/x-python", 1
    )
    return sk


@pytest.fixture
def fake_studio(monkeypatch):
    """把模块里的 get_studio_client 换成假客户端。"""
    from app.core import skill_exec as skill_exec_mod

    holder: dict[str, _FakeStudio] = {}

    def _install(studio: _FakeStudio) -> _FakeStudio:
        """装上假 studio 并返回它，便于用例链式取用。"""
        holder["studio"] = studio
        monkeypatch.setattr(skill_exec_mod, "get_studio_client", lambda: studio)
        return studio

    return _install


# --------------------------------------------------------------- 语言推断


@pytest.mark.parametrize(
    "path,expected",
    [
        ("scripts/a.py", "python"),
        ("scripts/a.sh", "bash"),
        ("scripts/a.bash", "bash"),
        ("scripts/a.js", "node"),
        ("scripts/a.mjs", "node"),
        ("scripts/a.PY", "python"),
    ],
)
def test_guess_language(path: str, expected: str):
    """按扩展名推断语言（studio 的运行时矩阵是 python / bash / node）。"""
    assert guess_language(path) == expected


def test_guess_language_rejects_unknown_suffix():
    """扩展名不认识 → 明确报错，不猜。"""
    with pytest.raises(SkillExecError) as ei:
        guess_language("scripts/a.rb")
    assert "语言" in str(ei.value)


# --------------------------------------------------------------- 路径校验


def test_validate_script_path_accepts_normal_path():
    """正常路径通过。"""
    assert validate_script_path("scripts/extract.py") == "scripts/extract.py"


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "scripts/../../etc/passwd",
        "/etc/passwd",
        "scripts\\run.py",
        "references/data.md",
        "",
    ],
)
def test_validate_script_path_rejects_bad_paths(bad: str):
    """上跳、绝对路径、反斜杠、非 scripts/ 目录——全拒。"""
    with pytest.raises(SkillExecError):
        validate_script_path(bad)


# --------------------------------------------------------------- 参数校验


def test_validate_argv_accepts_normal_args():
    """正常参数通过。"""
    assert validate_argv(["--flag", "value", "--n=1", "a b"]) == [
        "--flag",
        "value",
        "--n=1",
        "a b",
    ]
    assert validate_argv(None) == []
    assert validate_argv([]) == []


@pytest.mark.parametrize(
    "bad",
    [
        ["../../etc/passwd"],
        ["--in=../../etc/passwd"],
        ["/etc/passwd"],
        ["--out=/etc/passwd"],
        ["a\\b"],
        ["a\x00b"],
    ],
)
def test_validate_argv_rejects_traversal(bad: list[str]):
    """参数里的穿越式路径全拒。

    studio 收到的永远是**已校验过**的内容——这是 §22.13 的验收项。
    """
    with pytest.raises(SkillExecError):
        validate_argv(bad)


def test_validate_argv_rejects_wrong_shapes():
    """形状不对的参数也要拒。"""
    with pytest.raises(SkillExecError):
        validate_argv("not a list")  # type: ignore[arg-type]
    with pytest.raises(SkillExecError):
        validate_argv([1, 2])  # type: ignore[list-item]
    with pytest.raises(SkillExecError):
        validate_argv(["x"] * 65)


# --------------------------------------------------------------- 读脚本


async def test_read_script_returns_content(db):
    """脚本正文从数据库里读出来。"""
    sk = await _make_skill(db, "print(1)")
    content = await read_script(db, sk.code, "scripts/run.py", 200000)
    assert content == "print(1)"


async def test_read_script_missing_skill(db):
    """技能不存在 → 报错。"""
    with pytest.raises(SkillExecError) as ei:
        await read_script(db, "nope", "scripts/run.py", 200000)
    assert "不存在" in str(ei.value)


async def test_read_script_missing_resource(db):
    """没有这个文件 → 报错。"""
    sk = await _make_skill(db)
    with pytest.raises(SkillExecError) as ei:
        await read_script(db, sk.code, "scripts/missing.py", 200000)
    assert "没有资源" in str(ei.value)


async def test_read_script_rejects_binary(db):
    """二进制资源不能当脚本跑。"""
    sk = await _make_skill(db)
    await skill_repo.put_resource(
        db, sk.id, "scripts/blob.py", b"\x00\x01", "application/octet-stream", 0
    )
    with pytest.raises(SkillExecError) as ei:
        await read_script(db, sk.code, "scripts/blob.py", 200000)
    assert "二进制" in str(ei.value)


async def test_read_script_rejects_oversized(db):
    """超长脚本拒掉。"""
    sk = await _make_skill(db, "x" * 100)
    with pytest.raises(SkillExecError) as ei:
        await read_script(db, sk.code, "scripts/run.py", 10)
    assert "上限" in str(ei.value)


# --------------------------------------------------------------- 结果格式


def test_format_exec_result_success_shape():
    """成功时的形状取自 §22.8.4 的例子。"""
    text = format_exec_result(
        ExecResult("completed", 0, '{"rows": 128}', "(warning) 2 skipped", False, 12, "", "d")
    )
    assert text.startswith("exit_code: 0")
    assert "--- stdout ---" in text
    assert '{"rows": 128}' in text
    assert "--- stderr ---" in text
    assert "2 skipped" in text


def test_format_exec_result_failure_shape():
    """失败时也要说清状态与原因。"""
    text = format_exec_result(
        ExecResult("timeout", -1, "", "boom", False, 30000, "执行超时", "")
    )
    assert "status: timeout" in text
    assert "error: 执行超时" in text
    assert "exit_code: -1" in text


def test_format_exec_result_marks_truncation():
    """输出被截断要说出来。"""
    text = format_exec_result(ExecResult("completed", 0, "x", "", True, 5, "", "d"))
    assert "截断" in text


# --------------------------------------------------------------- 执行


async def test_exec_without_studio_is_refused(db, monkeypatch):
    """studio 没配置 → 明确说不启用。"""
    from app.core import skill_exec as skill_exec_mod

    monkeypatch.setattr(skill_exec_mod, "get_studio_client", lambda: None)
    ok, text = await exec_skill_script(
        db, skill="x", script="scripts/a.py", allowed_codes=None
    )
    assert ok is False
    assert "未启用" in text


async def test_exec_rejects_unbound_skill(db, fake_studio):
    """没绑定的技能不能跑它的脚本。"""
    fake_studio(_FakeStudio())
    sk = await _make_skill(db)
    ok, text = await exec_skill_script(
        db, skill=sk.code, script="scripts/run.py", allowed_codes={"other"}
    )
    assert ok is False
    assert "授权列表" in text


async def test_exec_rejects_bad_path_without_calling_studio(db, fake_studio):
    """路径不合法 → **请求根本没发出去 studio**。"""
    studio = fake_studio(_FakeStudio())
    ok, text = await exec_skill_script(
        db, skill="x", script="../../etc/passwd", allowed_codes=None
    )
    assert ok is False
    assert studio.calls == [], "校验没过就不该发出请求"


async def test_exec_rejects_bad_argv_without_calling_studio(db, fake_studio):
    """参数不合法 → **请求根本没发出去 studio**。"""
    studio = fake_studio(_FakeStudio())
    ok, text = await exec_skill_script(
        db, skill="x", script="scripts/a.py", argv=["../../etc/passwd"], allowed_codes=None
    )
    assert ok is False
    assert studio.calls == [], "校验没过就不该发出请求"


async def test_exec_success_returns_stdout_and_exit_code(db, fake_studio):
    """成功：退出码与 stdout 原样回灌。"""
    studio = fake_studio(
        _FakeStudio(ExecResult("completed", 0, '{"rows": 128}', "", False, 9, "", "d"))
    )
    sk = await _make_skill(db, "print(1)")
    ok, text = await exec_skill_script(
        db, skill=sk.code, script="scripts/run.py", allowed_codes={sk.code}
    )
    assert ok is True
    assert "exit_code: 0" in text
    assert '{"rows": 128}' in text
    # 请求里带的是脚本正文与审计字段
    call = studio.calls[0]
    assert call["content"] == "print(1)"
    assert call["script"] == "scripts/run.py"
    assert call["language"] == "python"
    assert call["skill"] == sk.code
    assert call["request_id"]


async def test_exec_nonzero_exit_is_not_a_service_error(db, fake_studio):
    """脚本 exit 1 → 结果回灌，但不是「服务不可用」。"""
    fake_studio(_FakeStudio(ExecResult("completed", 1, "", "Traceback...", False, 5, "", "d")))
    sk = await _make_skill(db)
    ok, text = await exec_skill_script(
        db, skill=sk.code, script="scripts/run.py", allowed_codes={sk.code}
    )
    assert ok is False
    assert "exit_code: 1" in text
    assert "Traceback" in text


async def test_exec_studio_unavailable_is_fed_back(db, fake_studio):
    """studio 不可达 → 回灌给模型，**不失败节点**（§17.5）。"""
    fake_studio(_FakeStudio(error=StudioUnavailableError("连不上 studio")))
    sk = await _make_skill(db)
    ok, text = await exec_skill_script(
        db, skill=sk.code, script="scripts/run.py", allowed_codes={sk.code}
    )
    assert ok is False
    assert "连不上 studio" in text


async def test_exec_rejects_language_studio_does_not_support(db, fake_studio):
    """studio 不支持这个运行时 → 请求不发出去。

    skill-service §10.2 的「写的时候严格」：不支持的语言要在调用前就拒。
    """
    studio = fake_studio(_FakeStudio(languages=("bash",)))
    sk = await _make_skill(db)
    ok, text = await exec_skill_script(
        db, skill=sk.code, script="scripts/run.py", allowed_codes={sk.code}
    )
    assert ok is False
    assert "bash" in text
    assert studio.calls == []


# --------------------------------------------------------------- 工具注册


def test_skill_exec_is_not_registered_by_default():
    """默认**不注册**。

    studio 没配置时脚本执行能力整体不存在——工具压根不该出现（模型看不见、
    配置期也拒），而不是「能勾上但每次调用都失败」。
    """
    from app.core.tools import get_tool, tool_names

    assert get_tool("skill_exec") is None
    assert "skill_exec" not in tool_names()


def test_optional_tool_registration_toggles():
    """开关注册 / 注销可选工具。"""
    from app.core.tools import get_tool, set_optional_tools_enabled, tool_names

    try:
        set_optional_tools_enabled(True)
        spec = get_tool("skill_exec")
        assert spec is not None
        assert "skill_exec" in tool_names()
        assert spec.parameters["required"] == ["skill", "script"]
        assert spec.parameters["additionalProperties"] is False
    finally:
        set_optional_tools_enabled(False)
    assert get_tool("skill_exec") is None


def test_skill_exec_cannot_be_configured_when_unavailable():
    """studio 不可用时，保存期拒掉 ``skill_exec``，并说明原因。

    §22.13 的验收项：「保存期拒绝把 skill_exec 加进 Agent 的工具白名单」。
    """
    from app.core.tools import ToolConfigError, normalize_tool_names, set_optional_tools_enabled

    try:
        set_optional_tools_enabled(False)
        with pytest.raises(ToolConfigError) as ei:
            normalize_tool_names(["skill_exec"])
        assert "studio" in str(ei.value)

        set_optional_tools_enabled(True)
        assert normalize_tool_names(["skill_exec"]) == ["skill_exec"]
    finally:
        set_optional_tools_enabled(False)


# --------------------------------------------------- 技能 code 怎么告诉模型


async def _skill_param_desc(codes: set[str] | None, tool: str = "skill_exec") -> str:
    """按给定的技能白名单展开一次工具 schema，返回 ``skill`` 参数的描述。

    Args:
        codes: 技能白名单；``None`` 表示不限制。
        tool: 工具名。

    Returns:
        ``skill`` 参数的描述文本。
    """
    from app.core.skill_context import (
        reset_current_skill_codes,
        set_current_skill_codes,
    )
    from app.core.tools import build_openai_tools, set_optional_tools_enabled

    set_optional_tools_enabled(True)
    token = set_current_skill_codes(codes)
    try:
        specs = await build_openai_tools([tool])
        assert specs is not None
        return specs[0]["function"]["parameters"]["properties"]["skill"]["description"]
    finally:
        reset_current_skill_codes(token)
        set_optional_tools_enabled(False)


@pytest.mark.parametrize("tool", ["skill_exec", "skill_resource"])
async def test_skill_param_lists_the_actual_bound_codes(tool: str):
    """``skill`` 参数的描述里要列出**实际可用的技能 code**。

    模型判断「调哪个技能」的唯一依据就是提示词与工具 schema。技能正文（§22.6）
    注入的是正文，正文里不一定写着自己的 code；原先 schema 里只有一句举例。
    于是真模型只能**猜**——实测（真 MiniMax + 真部署）它连猜了十来个
    （``hello``、``hello-script``、``hello-world``…），全被白名单拒掉，最后
    撞上「工具调用超过 5 轮」失败。

    这条钉住的就是：**可用的 code 必须出现在模型看得见的地方**。
    """
    desc = await _skill_param_desc({"financial-report", "quarterly-metrics"})
    assert "financial-report" in desc
    assert "quarterly-metrics" in desc


async def test_skill_param_says_do_not_call_when_no_skills_bound():
    """一个技能都没绑时，描述要明确说「别调这个工具」。

    空列表的 schema 比泛泛的描述更能压住模型——它看到的是「本 Agent 没有技能」，
    而不是「举例 financial-report」，于是不会去猜。
    """
    desc = await _skill_param_desc(set())
    assert "没有绑定任何技能" in desc
    assert "不要调用" in desc


async def test_skill_param_description_does_not_leak_between_contexts():
    """动态描述**不能**写回注册表——否则跨节点串味。

    注册表里的 ``parameters`` 是全局共享的那份 dict。一次 run 里多个节点并发跑、
    绑不同的技能时，改了它就是互相污染：节点 A 的模型会看到节点 B 的技能列表。
    """
    from app.core.skill_context import (
        reset_current_skill_codes,
        set_current_skill_codes,
    )
    from app.core.tools import build_openai_tools, set_optional_tools_enabled

    set_optional_tools_enabled(True)
    try:
        token = set_current_skill_codes({"secret-skill-a"})
        try:
            await build_openai_tools(["skill_exec"])
        finally:
            reset_current_skill_codes(token)

        # 换个上下文再展开一次：描述应当回到「通用」那一版
        specs = await build_openai_tools(["skill_exec"])
        assert specs is not None
        desc = specs[0]["function"]["parameters"]["properties"]["skill"]["description"]
        assert "secret-skill-a" not in desc

        # 注册表本身也没被改
        from app.core.tools import get_tool

        spec = get_tool("skill_exec")
        assert spec is not None
        assert "secret-skill-a" not in spec.parameters["properties"]["skill"]["description"]
    finally:
        set_optional_tools_enabled(False)


async def test_non_skill_tools_keep_their_static_schema():
    """只有技能类工具会被改写——别的工具的 schema 一字不动。"""
    from app.core.skill_context import (
        reset_current_skill_codes,
        set_current_skill_codes,
    )
    from app.core.tools import build_openai_tools, set_optional_tools_enabled

    set_optional_tools_enabled(True)
    token = set_current_skill_codes({"some-skill"})
    try:
        specs = await build_openai_tools(["calc"])
        assert specs is not None
        assert "some-skill" not in specs[0]["function"]["description"]
    finally:
        reset_current_skill_codes(token)
        set_optional_tools_enabled(False)
