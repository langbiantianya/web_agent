"""内置工具注册表（V3.2，设计文档 §17）。

## 工具从哪来

**只从代码里来。** agent 的 ``tools`` 字段填的是**工具名**，服务端从注册表里取
schema 展开给模型，执行逻辑也从注册表里取。

配置里不能定义执行逻辑——那等于 RCE。本项目已有同样的先例：条件表达式不用
``eval``（:mod:`app.core.expr`），而工具参数的危险等级只比条件表达式更高：
参数直接来自**模型**，而模型会被提示词注入影响。

## 为什么不让用户直接写 OpenAI 的 function schema

透传 schema 的话，模型可以调一个服务端根本不认识的函数，拿到什么结果全看
实现。注册表把「模型能调的」与「服务端能执行的」绑成同一张表：注册表里没有
的名字**保存期就拒绝**，配置根本存不进去。

## 工具名即契约

``tools`` 里写的是名字，schema 与执行器都从同一张表取，因此「模型看到的描述」
与「服务端实际做的事」不可能对不上——这是透传方案的根本缺陷。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.arith import ArithError, evaluate
from app.core.errors import BadRequestError

logger = logging.getLogger(__name__)

# 工具执行器：吃一份已解析的参数，返回一段给模型看的文本
ToolHandler = Callable[[dict], Awaitable[str]]


class ToolConfigError(BadRequestError):
    """agent 的 tools 配置不合法——保存期就拒。"""


class ToolCallError(Exception):
    """一次工具调用失败——参数不合法，或工具自己算不出来。

    与 :class:`ToolConfigError` 分开：配置错误是**保存期**的事（配置根本不该
    存在），而这是**运行期**的事（模型这次调错了）。

    工具执行器用抛出异常来表达失败，而不是返回一段错误字符串——返回值里混着
    「算出来的内容」和「出错的说明」，调用方分不清。异常统一在
    :func:`execute_tool` 里被转成 ``(False, 说明)``。
    """


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个内置工具。

    Attributes:
        name: 工具名。agent 的 ``tools`` 字段填的就是它。
        description: 给模型看的描述——**模型据此决定调不调、怎么调**。
        parameters: JSON Schema（OpenAI function parameters 格式）。
        handler: 异步执行函数。返回给模型看的文本。
    """

    name: str
    description: str
    parameters: dict
    handler: ToolHandler

    def to_openai(self) -> dict:
        """转成 OpenAI function-calling 的 ``tools`` 条目。

        Returns:
            ``{"type": "function", "function": {...}}`` 形式的字典。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------- 内置工具


async def _now(args: dict) -> str:
    """当前时间。

    Args:
        args: 无参数，这里不用。

    Returns:
        UTC 与本地时间的多行文本。
    """
    now = datetime.now(UTC)
    return (
        f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"ISO: {now.isoformat(timespec='seconds')}\n"
        f"本地: {now.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )


async def _calc(args: dict) -> str:
    """算术求值。

    Args:
        args: 需含 ``expression``（算术表达式字符串）。

    Returns:
        计算结果的文本。

    Raises:
        ToolCallError: 缺少参数，或表达式非法。异常由 :func:`execute_tool` 统一
            转成「失败结果」回灌给模型，而不是在这里返回一段错误字符串——
            否则调用方分不清「算出来是这句话」和「算崩了是这句话」。
    """
    expr = args.get("expression")
    if not isinstance(expr, str) or not expr.strip():
        raise ToolCallError(
            "缺少 expression 参数（一个算术表达式字符串，例如 (12 + 8) * 3）"
        )
    try:
        value = evaluate(expr)
    except ArithError as exc:
        raise ToolCallError(str(exc)) from exc
    return str(value)


# 注册表：名字 → 工具。**加新工具只需要在这里加一条**。
TOOL_REGISTRY: dict[str, ToolSpec] = {
    "now": ToolSpec(
        name="now",
        description=(
            "获取当前时间（UTC 与本地时区）。写与时间相关的内容、"
            "或需要判断「今天/最近」时先查这个。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_now,
    ),
    "calc": ToolSpec(
        name="calc",
        description=(
            "计算一个算术表达式，支持 + - * / // % ** 与括号。"
            "只做算术，不支持变量、函数或比较。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "算术表达式，例如 (12 + 8) * 3",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        handler=_calc,
    ),
}


# ----------------------------------------------------------------- 对外接口


def tool_names() -> list[str]:
    """注册表里可用的工具名（已排序）。

    Returns:
        工具名列表。
    """
    return sorted(TOOL_REGISTRY)


def get_tool(name: str) -> ToolSpec | None:
    """按名字取工具。

    Args:
        name: 工具名。

    Returns:
        对应的 :class:`ToolSpec`；没注册过则 ``None``。
    """
    return TOOL_REGISTRY.get(name)


def normalize_tool_names(raw: Any) -> list[str] | None:
    """把 agent 的 ``tools`` 配置规整成「已注册的工具名」列表。

    接受 ``None``（不用工具）、字符串数组、单个字符串。不认识的工具名
    **在这里就报错**——保存期拦住，配置根本进不了库。

    Args:
        raw: 原始配置值。

    Returns:
        去重后的工具名列表；没有工具时返回 ``None``。

    Raises:
        ToolConfigError: 配置不是工具名，或工具名未注册。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ToolConfigError("tools 必须是工具名的数组")

    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ToolConfigError(
                "tools 里只能放工具名（字符串），不能放函数定义——"
                f"可用的工具：{'、'.join(tool_names())}"
            )
        name = item.strip()
        if not name:
            continue
        if name not in TOOL_REGISTRY:
            raise ToolConfigError(
                f"未知工具「{name}」，可用的有：{'、'.join(tool_names())}"
            )
        if name not in names:
            names.append(name)
    return names or None


def known_tool_names(raw: Any) -> list[str] | None:
    """宽容地读出库里存的工具名——**只读，不报错**。

    与 :func:`normalize_tool_names` 相反的方向：那个用于**写入**（配置不合法就
    拒绝），这个用于**读取**（历史数据可能脏，但不能让整个列表接口 500）。

    脏数据长什么样：`tools` 字段在 V3.2 之前是没人用的占位，有人可能往里塞过
    OpenAI 的 function schema（dict），也可能有拼错的名字。读的时候一律丢掉
    不认识的部分。

    Args:
        raw: 库里存的值（通常是 list，也可能是 None 或别的形状）。

    Returns:
        已注册的工具名列表；一个都没有时返回 ``None``。
    """
    if not isinstance(raw, list):
        return None
    names = [n for n in raw if isinstance(n, str) and n in TOOL_REGISTRY]
    # 去重但保持原有顺序
    return list(dict.fromkeys(names)) or None


def build_openai_tools(names: list[str] | None) -> list[dict] | None:
    """把工具名列表展开成 OpenAI 的 ``tools`` 参数。

    **先按字符串过滤**：``tools`` 列是 JSON，V3.2 之前存过 OpenAI 的 function
    schema（dict）。曾经这里直接 ``TOOL_REGISTRY.get(name)``，遇到 dict 会抛
    ``TypeError: cannot use 'dict' as a dict key``——而这个异常在运行期被编排器
    当成「节点失败」重试 3 次，最后 run 以一条看不懂的 TypeError 收尾。
    读路径上有 :func:`known_tool_names` 做同样的宽容处理，运行期这里也要有。

    Args:
        names: 工具名列表。

    Returns:
        OpenAI function-calling 的 tools 数组；没有可用工具时返回 ``None``。
    """
    if not names:
        return None
    specs: list[dict] = []
    for name in names:
        if not isinstance(name, str):
            # 脏数据（V3.2 之前的 function schema）：读路径已经宽容过，
            # 运行期也必须宽容——这里抛出去会让整个节点失败。
            logger.warning("跳过非法工具配置：%r", name)
            continue
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            # 保存期已经拦过；这里仍兜一层，防止直接写库塞进非法配置
            logger.warning("跳过未注册的工具：%s", name)
            continue
        specs.append(spec.to_openai())
    return specs or None


async def execute_tool(
    name: str, arguments: str | dict, allowed: list[str] | None = None
) -> tuple[bool, str]:
    """执行一个工具，返回 ``(是否成功, 给模型看的结果文本)``。

    **错误不抛出去，而是作为结果回给模型**：模型看到「参数错了」有机会自己纠正
    （换个参数、或者干脆不用这个工具），比直接让整个节点失败有用得多。真正兜不住
    的是「模型一直调同一个必失败的工具」，那由轮次上限收口。

    Args:
        name: 模型要调的工具名。
        arguments: 工具参数。模型给的是 JSON 字符串，这里也接受已解析的 dict。
        allowed: 该 Agent **被授权**的工具名白名单。给了就只允许调这些——
            模型只能看到被展开进 ``tools`` 参数的那些工具，但「模型请求的工具名」
            与「服务端执行的工具名」仍要在这里对齐一次：提示词注入可以让模型
            报出一个没被授权的名字，那不该被执行。

    Returns:
        ``(ok, text)``。``ok`` 为 False 时 ``text`` 是给模型看的错误说明。
    """
    if allowed is not None and name not in allowed:
        return False, f"工具「{name}」不在本 Agent 的授权列表里，不能调用。"
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return False, (
            f"错误：没有名为「{name}」的工具。可用的工具：{'、'.join(tool_names())}"
        )

    try:
        args = _parse_args(arguments)
    except ToolCallError as exc:
        return False, f"错误：{exc}"

    try:
        return True, await spec.handler(args)
    except ToolCallError as exc:
        # 参数/输入不合法：消息本身就是给模型看的说明，原样回灌
        return False, f"错误：{exc}"
    except Exception as exc:  # noqa: BLE001 - 工具是第三方边界，异常一律回灌给模型
        logger.warning("工具 %s 执行失败：%s", name, exc)
        return False, f"错误：工具执行失败（{type(exc).__name__}）：{exc}"


def _parse_args(arguments: str | dict) -> dict:
    """解析工具参数。

    Args:
        arguments: JSON 字符串或已解析的 dict。

    Returns:
        参数字典。

    Raises:
        ToolCallError: 不是合法 JSON，或顶层不是对象。
    """
    if isinstance(arguments, dict):
        return arguments
    if not arguments or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise ToolCallError(f"参数不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ToolCallError(
            f"参数必须是 JSON 对象，收到的是 {type(parsed).__name__}"
        )
    return parsed
