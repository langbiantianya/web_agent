"""MCP 命名映射与结果映射（设计文档 §21.5、§21.9）。

两块纯函数，各自钉住一条不能破的约定：

1. **命名映射** —— ``mcp__<server_code>__<tool>``。OpenAI 的函数名约束是
   ``^[A-Za-z0-9_-]{1,64}$``（**没有点号**），所以两台服务器的同名工具必须靠
   server code 隔开；超长的名字**不导入、不截断**——截断会把两个不同的工具
   撞成同一个名，那比不导入更糟。
2. **结果映射** —— MCP 的 ``CallToolResult`` → 本项目的 ``(ok, text)``。
   ``isError`` **不失败节点**（错误文本回灌给模型），非文本内容给占位说明而不是
   空字符串。
"""

from __future__ import annotations

import json

import pytest

from app.core.mcp.mapping import map_call_result
from app.core.mcp.naming import (
    MCP_PREFIX,
    OPENAI_NAME_PATTERN,
    McpNamingError,
    build_qualified_name,
    is_mcp_name,
    parse_qualified_name,
)


# --------------------------------------------------------------- 命名映射


def test_build_qualified_name_shape():
    """限定名的形状是 ``mcp__<server_code>__<tool>``。

    server_code 用的是**本项目里**的 code，不是 MCP 协议里的 ``serverInfo.name``——
    后者是服务端自报的，不同服务器可能重名，不能拿来当主键（§21.5）。
    """
    assert build_qualified_name("weather", "get_weather") == "mcp__weather__get_weather"
    assert build_qualified_name("srv", "t") == "mcp__srv__t"


def test_qualified_name_satisfies_openai_constraint():
    """拼出来的名字必须过 OpenAI 的函数名正则。

    这是硬约束：不合规的名字发给模型会被直接拒，而 MCP 服务器自报的名字我们
    改不了——所以不合规的**不导入**，而不是导入后再想办法。
    """
    name = build_qualified_name("weather", "get_weather")
    assert OPENAI_NAME_PATTERN.match(name)
    assert len(name) <= 64


def test_build_qualified_name_rejects_dots():
    """工具名里有点号 → 拒绝。

    OpenAI 的函数名不允许点号。这不是「可以接受的损失」，而是必须拦下的：名字
    不合规的话，模型请求这个工具时会被 API 拒掉，表现成「工具明明在列表里却
    调不通」。
    """
    with pytest.raises(McpNamingError) as ei:
        build_qualified_name("weather", "get.weather")
    assert "OpenAI" in str(ei.value)


def test_build_qualified_name_rejects_overlong_names():
    """超长的工具名不导入、不截断。"""
    long_tool = "t" * 100
    with pytest.raises(McpNamingError):
        build_qualified_name("weather", long_tool)
    # 边界：正好 64 字符是允许的
    server = "srv"
    tool = "t" * (64 - len(MCP_PREFIX) - len(server) - 2)
    assert len(build_qualified_name(server, tool)) == 64


def test_parse_qualified_name_round_trip():
    """拼出来再拆回去要还原。"""
    assert parse_qualified_name("mcp__weather__get_weather") == (
        "weather",
        "get_weather",
    )
    # 工具名自己带分隔符时，只切第一个
    assert parse_qualified_name("mcp__weather__a__b") == ("weather", "a__b")


def test_parse_qualified_name_rejects_bad_shapes():
    """形状不对的限定名 → 报错，不猜。"""
    for bad in ("weather__get", "mcp__", "mcp__weather__", "mcp____x", "calc"):
        with pytest.raises(McpNamingError):
            parse_qualified_name(bad)


def test_is_mcp_name():
    """前缀判定。"""
    assert is_mcp_name("mcp__weather__get_weather")
    assert not is_mcp_name("calc")
    assert not is_mcp_name("")
    assert not is_mcp_name(None)  # type: ignore[arg-type]


# --------------------------------------------------------------- 结果映射


class _Fake:
    """造一个假的 MCP 内容项。"""

    def __init__(self, **kwargs) -> None:
        """按关键字赋值。"""
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_map_text_result():
    """普通文本 → ``(True, 文本)``。"""
    result = _Fake(is_error=False, content=[_Fake(type="text", text="hello")])
    ok, text = map_call_result(result)
    assert ok is True
    assert text == "hello"


def test_map_joins_multiple_text_blocks():
    """多段文本用换行连接，顺序保留。"""
    result = _Fake(
        is_error=False,
        content=[
            _Fake(type="text", text="第一段"),
            _Fake(type="text", text="第二段"),
        ],
    )
    ok, text = map_call_result(result)
    assert ok is True
    assert text == "第一段\n第二段"


def test_map_is_error_does_not_raise():
    """``isError: true`` → ``ok=False``，但**不抛异常**。

    这与 §17.5 同一口径：工具执行错误不是节点失败，模型看到错误文本有机会自己
    纠正。抛异常会让整个节点挂掉，白白丢掉这一次交互机会。
    """
    result = _Fake(is_error=True, content=[_Fake(type="text", text="boom")])
    ok, text = map_call_result(result)
    assert ok is False
    assert text == "boom"


def test_map_falls_back_to_structured_content():
    """没有 content 但有 structuredContent → 用它的 JSON 序列化。

    规范要求有 structured 时**同时**给一份 text，但第三方服务器未必照做。
    """
    result = _Fake(is_error=False, content=[], structured_content={"a": 1})
    ok, text = map_call_result(result)
    assert ok is True
    assert json.loads(text) == {"a": 1}


def test_map_image_becomes_placeholder():
    """图片 → 占位说明，**不是空字符串**。

    空字符串会让模型以为「工具跑了但什么都没返回」——它于是可能反复重试同一个
    工具，白烧轮次。占位说明把「有东西但这个通道传不了」讲清楚。
    """
    result = _Fake(
        is_error=False,
        content=[_Fake(type="image", mimeType="image/png", data="AAAA")],
    )
    ok, text = map_call_result(result)
    assert ok is True
    assert "图片" in text
    assert "image/png" in text
    assert text.strip() != ""


def test_map_audio_and_resource_link_become_placeholders():
    """音频与资源链接同样给占位说明。"""
    result = _Fake(
        is_error=False,
        content=[
            _Fake(type="audio", mimeType="audio/wav", data="AAAA"),
            _Fake(type="resource_link", uri="file:///tmp/a.txt"),
        ],
    )
    ok, text = map_call_result(result)
    assert ok is True
    assert "音频" in text
    assert "资源链接" in text


def test_map_embedded_text_resource_is_extracted():
    """``EmbeddedResource`` 里是 text 资源时，文本要取出来用。"""
    result = _Fake(
        is_error=False,
        content=[
            _Fake(
                type="resource",
                resource=_Fake(mimeType="text/plain", text="内嵌正文", uri="file:///a.txt"),
            )
        ],
    )
    ok, text = map_call_result(result)
    assert ok is True
    assert "内嵌正文" in text


def test_map_truncates_overlong_results():
    """超长结果截断——工具结果要回灌进下一轮的 prompt，不能把它撑爆。"""
    result = _Fake(is_error=False, content=[_Fake(type="text", text="x" * 5000)])
    ok, text = map_call_result(result, max_chars=100)
    assert ok is True
    assert len(text) < 200
    assert "截断" in text


def test_map_empty_result_is_not_empty_text():
    """什么都没有时也给一句话，不给空串。"""
    result = _Fake(is_error=False, content=[])
    ok, text = map_call_result(result)
    assert ok is True
    assert text.strip() != ""
