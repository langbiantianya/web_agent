"""内置工具与算术求值（V3.2，设计文档 §17）。

分三块钉住：
1. **算术求值**：算得对，且危险构造一律拒；
2. **工具注册表**：工具名即契约——未注册的名字存不进配置；
3. **工具执行**：出错不抛，作为结果回灌给模型。

第 3 条是刻意的设计：**工具失败不等于节点失败**。模型看到「参数错了」有机会
自己纠正，比直接让整个节点挂掉有用得多。真正兜不住的是「模型一直调同一个
必失败的工具」，那由轮次上限收口（见 :mod:`tests.test_tool_e2e`）。
"""

from __future__ import annotations

import pytest

from app.core.arith import (
    MAX_EXPONENT,
    MAX_EXPR_LEN,
    ArithError,
    evaluate,
)
from app.core.tools import (
    ToolConfigError,
    build_openai_tools,
    execute_tool,
    get_tool,
    known_tool_names,
    normalize_tool_names,
    tool_names,
)


# --------------------------------------------------------------- 算术求值


def test_evaluate_basic_arithmetic():
    """四则运算与优先级、括号要算对。

    工具的价值全在「算对」上——算错了模型就会基于错数字编答案，比没有工具更糟。
    """
    assert evaluate("1 + 2 * 3") == 7
    assert evaluate("(1 + 2) * 3") == 9
    assert evaluate("10 // 3") == 3
    assert evaluate("10 % 3") == 1
    assert evaluate("2 ** 10") == 1024
    assert evaluate("-5 + 3") == -2
    assert evaluate("7 / 2") == 3.5


def test_evaluate_rejects_non_arithmetic_constructs():
    """变量、函数调用、比较、属性访问——一律拒。

    工具参数直接来自模型，而模型会被提示词注入影响。这里放行任何一种构造，
    等于把任意代码执行暴露给任何能写 prompt 的人。

    注释不在此列——``1 + 2 # 注释`` 里的注释会被词法分析直接丢掉，不构成任何
    执行风险，留着反而会让「白名单」的说法不准确。
    """
    dangerous = [
        "__import__('os').system('echo hi')",
        "open('/etc/passwd').read()",
        "x + 1",                      # 变量
        "abs(-1)",                    # 函数调用
        "1 < 2",                      # 比较
        "1 if True else 2",           # 条件表达式
        "[1, 2, 3]",                  # 列表
        "'abc'",                      # 字符串常量
        "True",                       # 布尔常量（int 的子类，明确排除）
        "1; 2",                       # 多条语句
        "().__class__",
    ]
    # 逐条收集「被放行」的表达式，失败时能直接看出是哪一条
    accepted = []
    for expr in dangerous:
        try:
            evaluate(expr)
        except ArithError:
            continue
        accepted.append(expr)
    assert accepted == [], f"以下构造被放行了：{accepted}"


def test_evaluate_rejects_resource_abuse():
    """长度、节点数、指数、结果量级——四道闸。

    表达式求值看着无害，但 ``2 ** 10000000`` 之类能把 CPU 和内存吃光。这里
    限制的是「一条消息里的一个参数」能造成的最大伤害。
    """
    # 指数上限：内层先算成 10^10，正好被挡住，不会真的去算天文数字
    with pytest.raises(ArithError, match="指数"):
        evaluate("10 ** 10 ** 10")
    with pytest.raises(ArithError, match="指数"):
        evaluate(f"2 ** {MAX_EXPONENT + 1}")
    # 指数必须整数
    with pytest.raises(ArithError, match="指数"):
        evaluate("2 ** 2.5")
    # 表达式过长
    with pytest.raises(ArithError, match="过长"):
        evaluate("1 + " * MAX_EXPR_LEN + "1")
    # 节点过多：连加 60 次 → 181 个 AST 节点，但长度只有 121 字符（先撞长度
    # 上限的话就不是「复杂」这条错误了）
    with pytest.raises(ArithError, match="复杂"):
        evaluate("1" + "+1" * 60)
    # 结果量级：2^100 有 101 位，连乘 11 次就是 1101 位，超过 1024 位上限
    with pytest.raises(ArithError, match="过大"):
        evaluate("2**100" + "*2**100" * 10)
    # 浮点溢出成 inf：不是「过大」而是「不是有限数」
    with pytest.raises(ArithError, match="有限"):
        evaluate("1e308 * 10")


def test_evaluate_error_messages_are_readable():
    """错误信息要能直接回灌给模型——「除数为零」比「ArithError」有用。"""
    with pytest.raises(ArithError) as ei:
        evaluate("1 / 0")
    assert "除数为零" in str(ei.value)
    with pytest.raises(ArithError) as ei:
        evaluate("1 +")
    assert "语法错误" in str(ei.value)
    with pytest.raises(ArithError) as ei:
        evaluate("   ")
    assert "空" in str(ei.value)


# --------------------------------------------------------------- 工具注册表


def test_registry_exposes_known_tools():
    """注册表里至少要有 now 与 calc，且每个都能取到 spec。"""
    names = tool_names()
    assert "now" in names
    assert "calc" in names
    for name in names:
        spec = get_tool(name)
        assert spec is not None
        assert spec.name == name
        assert spec.description
        assert spec.parameters["type"] == "object"


def test_tool_spec_to_openai_shape():
    """展开成 OpenAI function-calling 的形状——端点只认这个格式。"""
    spec = get_tool("calc")
    assert spec is not None
    entry = spec.to_openai()
    assert entry["type"] == "function"
    assert entry["function"]["name"] == "calc"
    assert entry["function"]["parameters"]["required"] == ["expression"]


def test_normalize_tool_names_accepts_known_names():
    """合法配置：去重、保序。"""
    assert normalize_tool_names(["now", "calc"]) == ["now", "calc"]
    assert normalize_tool_names(["calc", "calc", "now"]) == ["calc", "now"]
    assert normalize_tool_names("now") == ["now"]
    assert normalize_tool_names([]) is None
    assert normalize_tool_names(None) is None


def test_normalize_tool_names_rejects_unknown_names():
    """未注册的工具名**保存期就拒**。

    这是「工具名即契约」的全部意义：注册表里没有的名字，配置根本进不了库，
    也就不可能出现「模型能调、服务端执行不了」的工具。
    """
    with pytest.raises(ToolConfigError) as ei:
        normalize_tool_names(["nonexistent"])
    assert "未知工具" in str(ei.value)
    # 报错时要告诉用户有哪些可选，而不是只说「不合法」
    assert "calc" in str(ei.value)


def test_normalize_tool_names_rejects_function_definitions():
    """不许往 tools 里塞 OpenAI 的 function schema。

    透传 schema 的老做法等于让配置决定执行逻辑——那基本等同于 RCE。
    """
    with pytest.raises(ToolConfigError) as ei:
        normalize_tool_names([
            {"type": "function", "function": {"name": "shell"}},
        ])
    assert "不能放函数定义" in str(ei.value)


def test_known_tool_names_is_lenient_on_read():
    """读的时候宽容：脏数据不能把列表接口打成 500。

    写入（:func:`normalize_tool_names`）和读取（:func:`known_tool_names`）方向
    相反：写要严格，读要容错。
    """
    assert known_tool_names(["now", "calc"]) == ["now", "calc"]
    assert known_tool_names(["now", "不存在的", {"type": "function"}]) == ["now"]
    assert known_tool_names([{"type": "function"}]) is None
    assert known_tool_names(None) is None
    assert known_tool_names("不是列表") is None


def test_build_openai_tools_expands_and_skips_unknown():
    """展开成 schema；未注册的名字跳过并告警（兜底，不抛）。"""
    tools = build_openai_tools(["calc"])
    assert tools is not None
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "calc"
    # 全是未知名 → 没有可用工具 → None（而不是空数组）
    assert build_openai_tools(["不存在"]) is None
    assert build_openai_tools(None) is None
    assert build_openai_tools([]) is None


# --------------------------------------------------------------- 工具执行


async def test_execute_tool_now_returns_time():
    """now 返回可读的时间文本。"""
    ok, text = await execute_tool("now", "{}")
    assert ok is True
    assert "UTC" in text
    assert "ISO" in text


async def test_execute_tool_calc_computes():
    """calc 算出结果。"""
    ok, text = await execute_tool("calc", '{"expression": "(12 + 8) * 3"}')
    assert ok is True
    assert text == "60"


async def test_execute_tool_reports_errors_as_results_not_exceptions():
    """工具出错**不抛异常**，而是作为结果回灌。

    抛异常会让整个节点失败，而模型其实有机会自己纠正（换个参数、或者干脆
    不用这个工具）。回灌错误文本是给模型一次自我修正的机会。
    """
    ok, text = await execute_tool("calc", '{"expression": "1/0"}')
    assert ok is False
    assert "除数为零" in text

    ok, text = await execute_tool("calc", "{}")
    assert ok is False
    assert "expression" in text

    ok, text = await execute_tool("不存在的工具", "{}")
    assert ok is False
    assert "没有名为" in text


async def test_execute_tool_rejects_malformed_arguments():
    """参数不是合法 JSON、顶层不是对象——都当失败结果，不抛。"""
    ok, text = await execute_tool("calc", "{不是 JSON")
    assert ok is False
    assert "JSON" in text

    ok, text = await execute_tool("calc", "[1, 2]")
    assert ok is False
    assert "JSON 对象" in text

    # 空参数当成空对象：缺参数由工具自己报「缺少 expression」
    ok, text = await execute_tool("calc", "")
    assert ok is False
    assert "expression" in text
