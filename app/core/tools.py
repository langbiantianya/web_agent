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

## V4：MCP 工具进了同一张表（§21）

MCP 把「表的另一半」交给外部：工具的 schema 与执行器**天然同源**——MCP 服务器
自己声明参数形状、自己执行（§21.2）。所以这里不是「多了一个工具来源」，而是
**同一个契约的两种填表方式**。

名字形态上分成两类：

- 内置名（``calc``）——从 :data:`TOOL_REGISTRY` 取；
- MCP 限定名（``mcp__<server>__<tool>``）——从 ``mcp_tool`` 快照取。

四个对外函数（:func:`normalize_tool_names` / :func:`known_tool_names` /
:func:`build_openai_tools` / :func:`execute_tool`）都按这个前缀分流，**闭环本身
一个字都不改**（§21.13）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config.settings import settings
from app.core.arith import ArithError, evaluate
from app.core.errors import BadRequestError, SensitiveContentError
from app.core.mcp.naming import is_mcp_name
from app.core.skill_context import current_skill_codes

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


async def _skill_resource(args: dict) -> str:
    """读取技能包里的资源文件。

    内置工具的 handler **拿不到 Agent 参数**，所以「本 Agent 绑了哪些技能」
    是从 contextvars 里取的（:mod:`app.core.skill_context`）。没有那层限制的话，
    模型就能读任意技能的任意资源——技能之间是可以互相引用的，A 技能引了 B 技能
    的口径，B 的内容也就该只对绑了 B 的 Agent 可见。

    Args:
        args: 需含 ``skill``（技能 code）与 ``path``（资源相对路径）。

    Returns:
        资源文本；二进制资源返回一段说明而不是字节。

    Raises:
        ToolCallError: 参数缺失、技能未授权或不存在、路径非法。异常由
            :func:`execute_tool` 统一转成「失败结果」回灌给模型。
    """
    from app.config.db import SessionLocal
    from app.core.skill_context import current_skill_codes
    from app.core.skill_reader import read_skill_resource

    code = args.get("skill")
    path = args.get("path")
    if not isinstance(code, str) or not code.strip():
        raise ToolCallError("缺少 skill 参数（技能 code）")
    if not isinstance(path, str) or not path.strip():
        raise ToolCallError("缺少 path 参数（资源相对路径）")

    async with SessionLocal() as db:
        ok, text = await read_skill_resource(
            db,
            code.strip(),
            path,
            allowed_codes=current_skill_codes(),
            max_chars=settings.tool_result_max_chars,
        )
    if not ok:
        raise ToolCallError(text)
    return text


async def _skill_exec(args: dict) -> str:
    """在 studio 容器里执行技能脚本。

    与 :func:`_skill_resource` 同款：内置工具的 handler **拿不到 Agent 参数**，
    所以「本 Agent 绑了哪些技能」从 contextvars 里取（:mod:`app.core.skill_context`）。

    脚本正文**从数据库读出来随请求发给 studio**——studio 保持无状态，两边没有卷
    依赖（§22.8.3）。web_agent 进程内**不起任何子进程**：执行发生在 studio 容器
    里（§22.8.1）。

    Args:
        args: 需含 ``skill``（技能 code）与 ``script``（``scripts/`` 下的相对路径）；
            可选 ``args``（参数数组）、``stdin``、``timeout_ms``。

    Returns:
        给模型看的文本：退出码 + stdout + stderr。

    Raises:
        ToolCallError: 参数不合法、技能未授权、studio 不可用、脚本执行失败。异常
            由 :func:`execute_tool` 统一转成「失败结果」回灌给模型，而不是在这里
            返回一段错误字符串——否则调用方分不清「算出来是这句话」和「出错了」。
    """
    from app.config.db import SessionLocal
    from app.core.skill_context import current_skill_codes
    from app.core.skill_exec import exec_skill_script

    code = args.get("skill")
    script = args.get("script")
    if not isinstance(code, str) or not code.strip():
        raise ToolCallError("缺少 skill 参数（技能 code）")
    if not isinstance(script, str) or not script.strip():
        raise ToolCallError("缺少 script 参数（脚本相对路径，如 scripts/extract.py）")

    raw_argv = args.get("args")
    if raw_argv is not None and not isinstance(raw_argv, list):
        raise ToolCallError("args 必须是字符串数组")
    stdin = args.get("stdin") or ""
    if not isinstance(stdin, str):
        raise ToolCallError("stdin 必须是字符串")
    raw_timeout = args.get("timeout_ms")
    if raw_timeout is not None and not isinstance(raw_timeout, int):
        raise ToolCallError("timeout_ms 必须是整数")

    async with SessionLocal() as db:
        ok, text = await exec_skill_script(
            db,
            skill=code.strip(),
            script=script.strip(),
            argv=raw_argv,
            stdin=stdin,
            timeout_ms=raw_timeout,
            allowed_codes=current_skill_codes(),
        )
    # 无论成功还是失败，脚本输出都要过一遍敏感词过滤——失败的 stderr 里同样可能
    # 带敏感内容，而它照样会被回灌给模型（§22.13）。
    _screen_tool_output(text, "skill_exec")
    if not ok:
        raise ToolCallError(text)
    return text


def _screen_tool_output(text: str, tool_name: str) -> None:
    """工具结果回灌前的敏感词兜底（§20、§21.10 第 ④ 条、§22.13）。

    工具结果会**直接进 prompt**，它来自第三方（MCP 服务器、技能脚本），可能含
    提示词注入内容，也可能是敏感内容。§20 的过滤点在「内容进 LLM 之前」，
    工具结果与用户输入位置一样、性质也一样——不能只过滤用户输入。

    命中时按「拒绝」处理：**不回灌**，一路抛上去让 run 以敏感内容错误收尾。

    Args:
        text: 工具返回的文本。
        tool_name: 工具名（日志里用）。

    Raises:
        SensitiveContentError: 命中敏感词。
    """
    from app.core.sensitive import screen_text

    verdict = screen_text(text)
    if verdict is not None and verdict.hit:
        logger.warning(
            "工具 %s 的结果命中敏感词：分数 %.3f，位置 %d",
            tool_name,
            verdict.score,
            verdict.position,
        )
        raise SensitiveContentError(
            "工具返回的内容包含不被允许的信息，本次调用已中止",
            data={"score": verdict.score, "excerpt": verdict.excerpt},
        )


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
    "skill_resource": ToolSpec(
        name="skill_resource",
        description=(
            "读取技能包里的资源文件（references/ 下的参考文档、assets/ 下的附件）。"
            "技能正文里提到「详见 references/xxx」但没给内容时，用它按需取。"
            "只能读本 Agent 已绑定技能的资源。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "技能 code（不是显示名），例如 financial-report",
                },
                "path": {
                    "type": "string",
                    "description": "资源相对路径，例如 references/METRICS.md",
                },
            },
            "required": ["skill", "path"],
            "additionalProperties": False,
        },
        handler=_skill_resource,
    ),
}


# 可选工具：**只有在 studio 可用时才注册**（§22.8.6）。
#
# 为什么它不在 TOOL_REGISTRY 里直接写死：studio 没配置、或者配置了但连不上时，
# 脚本执行能力**整体不存在**——这时 `skill_exec` 应当**根本不出现**（模型看不见、
# 配置期也拒），而不是「能勾上但每次调用都失败」。后者会让模型反复重试同一个
# 坏工具，白烧轮次与 token。
OPTIONAL_TOOLS: dict[str, ToolSpec] = {
    "skill_exec": ToolSpec(
        name="skill_exec",
        description=(
            "在 studio 容器里执行技能包里的脚本（scripts/ 下的文件）。"
            "脚本跑在独立的受限容器里：没有凭据、不能出网、有时间与资源上限，"
            "跑挂了也不会影响主服务。参数以数组给出，不会被 shell 解析。"
            "返回退出码与 stdout/stderr。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "技能 code（不是显示名），例如 financial-report",
                },
                "script": {
                    "type": "string",
                    "description": "脚本相对路径，例如 scripts/extract.py",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "传给脚本的参数数组（不会被 shell 解析）",
                },
                "stdin": {
                    "type": "string",
                    "description": "标准输入（文本）",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "执行时限（毫秒）。服务端还有一道更严的上限，模型改不了",
                },
            },
            "required": ["skill", "script"],
            "additionalProperties": False,
        },
        handler=_skill_exec,
    ),
}


def set_optional_tools_enabled(enabled: bool) -> None:
    """按开关注册 / 注销可选工具。

    启动期调一次：studio 不可用就**不注册** ``skill_exec``（§22.8.6）。这样
    模型看不见它、配置期也拒——不会留下「配了但调不通」的哑配置。

    Args:
        enabled: 是否启用。
    """
    for name, spec in OPTIONAL_TOOLS.items():
        if enabled:
            TOOL_REGISTRY[name] = spec
        else:
            TOOL_REGISTRY.pop(name, None)


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


def normalize_tool_names(
    raw: Any, known_mcp: set[str] | None = None
) -> list[str] | None:
    """把 agent 的 ``tools`` 配置规整成「已注册的工具名」列表。

    接受 ``None``（不用工具）、字符串数组、单个字符串。不认识的工具名
    **在这里就报错**——保存期拦住，配置根本进不了库。

    Args:
        raw: 原始配置值。
        known_mcp: 已发现的 MCP 限定名集合。``None`` 表示「没有可用的 MCP 工具」
            （没查库，或一台都没发现）——此时任何 ``mcp__`` 名字都会被拒。

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
        if is_mcp_name(name):
            # MCP 限定名要到 ``mcp_tool`` 快照里查（§21.8）。服务器从未连过时
            # 快照是空的 → 400，并提示先刷新目录——宁可让配置保存失败并给出
            # 可执行的提示，也不要留一个「配了但模型看不见」的哑配置。
            if known_mcp is None or name not in known_mcp:
                raise ToolConfigError(
                    f"未知 MCP 工具「{name}」。它要么还不存在，要么所属的服务器"
                    "尚未发现工具——先到「MCP 服务器」里刷新一次工具目录。"
                )
        elif name not in TOOL_REGISTRY:
            if name in OPTIONAL_TOOLS:
                # 可选工具没注册 = studio 不可用。给一句能行动的提示，
                # 而不是把它混在「未知工具」里让人猜。
                raise ToolConfigError(
                    f"工具「{name}」当前不可用（studio 未启用或连不上），配不了它。"
                    "studio 恢复之后可以再配。"
                )
            raise ToolConfigError(
                f"未知工具「{name}」，可用的有：{'、'.join(tool_names())}"
            )
        if name not in names:
            names.append(name)
    return names or None


def known_tool_names(raw: Any, known_mcp: set[str] | None = None) -> list[str] | None:
    """宽容地读出库里存的工具名——**只读，不报错**。

    与 :func:`normalize_tool_names` 相反的方向：那个用于**写入**（配置不合法就
    拒绝），这个用于**读取**（历史数据可能脏，但不能让整个列表接口 500）。

    脏数据长什么样：`tools` 字段在 V3.2 之前是没人用的占位，有人可能往里塞过
    OpenAI 的 function schema（dict），也可能有拼错的名字。读的时候一律丢掉
    不认识的部分。

    Args:
        raw: 库里存的值（通常是 list，也可能是 None 或别的形状）。
        known_mcp: 已发现的 MCP 限定名集合。给了就把不在集合里的 MCP 名字丢掉
            （服务器被删了之后，残留的绑定不该继续显示）；没给就只做形状检查。

    Returns:
        可用的工具名列表；一个都没有时返回 ``None``。
    """
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        if is_mcp_name(name):
            if known_mcp is None or name in known_mcp:
                names.append(name)
            continue
        if name in TOOL_REGISTRY:
            names.append(name)
    # 去重但保持原有顺序
    return list(dict.fromkeys(names)) or None


# 带「技能 code」参数的内置工具。它们的 ``skill`` 参数描述要按**当前上下文**
# 动态生成——原因见 :func:`_skill_param_description`。
_SKILL_PARAM_TOOLS = ("skill_exec", "skill_resource")


def _skill_param_description(codes: set[str] | None) -> str:
    """生成 ``skill`` 参数的描述——把**实际可用的 code** 列出来。

    ## 为什么必须动态生成

    模型判断「该调哪个技能、脚本在哪」的唯一依据就是提示词与工具 schema。
    而技能正文（§22.6）注入的是**正文**，正文里不一定写着自己的 code；工具
    schema 里原先也只有一句举例（「例如 financial-report」）。

    于是真模型只能**猜** code。实测（真 MiniMax + 真部署）：模型连续猜了
    ``hello``、``hello-script``、``hello-world``… 十来个，全被白名单拒掉，
    最后撞上「工具调用超过 5 轮」失败。假 LLM 的测试测不到这个——它早就被
    写死成正确的 code 了。

    ## 为什么用执行侧同一份白名单

    这里列的 code 与 :func:`app.core.skill_context.current_skill_codes` 是**同一份**
    ——也就是执行期白名单校验用的那份。于是「模型看到的」与「服务端允许的」
    不可能对不上：被预算挡掉的技能压根不会出现在列表里（它压根没被注入）。

    Args:
        codes: 当前上下文允许的技能 code 集合；``None`` 表示不限制。

    Returns:
        给模型看的参数描述。
    """
    if codes is None:
        # 没有上下文（不在 run 里，比如后台的提示词预览）：保持通用描述。
        return "技能 code（不是显示名），例如 financial-report"
    if not codes:
        return (
            "技能 code。**本 Agent 当前没有绑定任何技能**——不要调用这个工具，"
            "直接回答用户即可。"
        )
    listed = "、".join(sorted(codes))
    return (
        f"技能 code（不是显示名）。本 Agent 当前可用的技能只有：{listed}。"
        "只能填这些 code，填别的会被直接拒绝。"
    )


def _openai_spec_for(spec: ToolSpec) -> dict:
    """把工具 spec 转成 OpenAI 条目；技能类工具按当前上下文改写 ``skill`` 参数。

    **不改注册表本身。** ``ToolSpec.parameters`` 是全局共享的那份 dict，直接改
    它就是跨节点串味——一次 run 里多个节点并发跑、绑不同的技能时，模型会看到
    别的节点的技能列表。所以这里逐层浅拷贝，只替换 ``skill`` 那一条。

    Args:
        spec: 工具 spec。

    Returns:
        OpenAI function-calling 的 ``tools`` 条目。
    """
    payload = spec.to_openai()
    if spec.name not in _SKILL_PARAM_TOOLS:
        return payload
    params = payload["function"].get("parameters")
    props = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(props, dict) or "skill" not in props:
        return payload
    new_props = dict(props)
    new_props["skill"] = {
        **new_props["skill"],
        "description": _skill_param_description(current_skill_codes()),
    }
    payload["function"]["parameters"] = {**params, "properties": new_props}
    return payload


async def build_openai_tools(names: list[str] | None) -> list[dict] | None:
    """把工具名列表展开成 OpenAI 的 ``tools`` 参数。
    schema（dict）。曾经这里直接 ``TOOL_REGISTRY.get(name)``，遇到 dict 会抛
    ``TypeError: cannot use 'dict' as a dict key``——而这个异常在运行期被编排器
    当成「节点失败」重试 3 次，最后 run 以一条看不懂的 TypeError 收尾。
    读路径上有 :func:`known_tool_names` 做同样的宽容处理，运行期这里也要有。

    MCP 工具（``mcp__`` 开头）的 schema 从 ``mcp_tool`` 快照取。**离线服务器的
    工具不展开**（§21.9）——模型看不见就不会去调一个调不通的工具；这比「照常
    展开、每次调用都失败」好：后者会让模型反复重试同一个坏工具，白烧轮次与
    token，最后撞上轮次上限失败。

    Args:
        names: 工具名列表。

    Returns:
        OpenAI function-calling 的 tools 数组；没有可用工具时返回 ``None``。
    """
    if not names:
        return None
    mcp_specs = await _mcp_openai_specs(names)
    specs: list[dict] = []
    for name in names:
        if not isinstance(name, str):
            # 脏数据（V3.2 之前的 function schema）：读路径已经宽容过，
            # 运行期也必须宽容——这里抛出去会让整个节点失败。
            logger.warning("跳过非法工具配置：%r", name)
            continue
        if is_mcp_name(name):
            spec = mcp_specs.get(name)
            if spec is None:
                logger.warning("跳过不可用的 MCP 工具：%s", name)
                continue
            specs.append(spec)
            continue
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            # 保存期已经拦过；这里仍兜一层，防止直接写库塞进非法配置
            logger.warning("跳过未注册的工具：%s", name)
            continue
        specs.append(_openai_spec_for(spec))
    return specs or None


async def _mcp_openai_specs(names: list[str]) -> dict[str, dict]:
    """把 MCP 工具名展开成 OpenAI function schema。

    只展开**在线**服务器的工具：离线服务器的工具仍在快照里（保存期校验、后台
    勾选框都要用），但不给模型看。

    Args:
        names: 工具名列表（只有 ``mcp__`` 开头的会被处理）。

    Returns:
        限定名 → OpenAI function 条目的字典。
    """
    wanted = [n for n in names if isinstance(n, str) and is_mcp_name(n)]
    if not wanted:
        return {}
    from app.config.db import SessionLocal
    from app.core.mcp.manager import mcp_manager
    from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo

    out: dict[str, dict] = {}
    async with SessionLocal() as db:
        rows = await mcp_tool_repo.list_all(db)
        servers = await mcp_server_repo.map_by_id(db)
    for row in rows:
        if row.qualified_name not in wanted:
            continue
        server = servers.get(row.server_id)
        if server is None or not server.enabled:
            continue
        if not mcp_manager.is_available(server.code):
            # 服务器不可用 → 不展开（§21.9）
            continue
        schema = row.input_schema or {"type": "object", "properties": {}}
        out[row.qualified_name] = {
            "type": "function",
            "function": {
                "name": row.qualified_name,
                "description": row.description or f"MCP 工具 {row.tool_name}",
                "parameters": schema,
            },
        }
    return out


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
    if is_mcp_name(name):
        # MCP 分支：名字 → 服务器 → tools/call（§21.9）。白名单已经校验过。
        return await _execute_mcp_tool(name, arguments)
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
    except SensitiveContentError:
        # 敏感内容**不回灌**（§20 的口径）：它一路往上抛，让 run 以敏感内容
        # 错误收尾，而不是把这段内容塞回给模型。
        raise
    except Exception as exc:  # noqa: BLE001 - 工具是第三方边界，异常一律回灌给模型
        logger.warning("工具 %s 执行失败：%s", name, exc)
        return False, f"错误：工具执行失败（{type(exc).__name__}）：{exc}"


async def _execute_mcp_tool(name: str, arguments: str | dict) -> tuple[bool, str]:
    """执行一个 MCP 工具。

    分发规则（§21.9）：``mcp__<server>__<tool>`` → 查服务器 → ``tools/call`` →
    结果映射成 ``(ok, text)``。

    Args:
        name: MCP 限定名。
        arguments: 工具参数。

    Returns:
        ``(ok, text)``。``ok`` 为 False 时 ``text`` 是给模型看的错误说明。
    """
    from app.config.db import SessionLocal
    from app.core.mcp.manager import mcp_manager
    from app.core.mcp.mapping import map_call_result
    from app.core.mcp.naming import parse_qualified_name
    from app.repo.mcp_repo import mcp_server_repo

    try:
        server_code, tool_name = parse_qualified_name(name)
    except ValueError as exc:
        return False, f"错误：{exc}"

    try:
        args = _parse_args(arguments)
    except ToolCallError as exc:
        return False, f"错误：{exc}"

    async with SessionLocal() as db:
        server = await mcp_server_repo.get_by_code(db, server_code)
    if server is None:
        return False, f"错误：MCP 服务器「{server_code}」不存在。"
    if not server.enabled:
        return False, f"错误：MCP 服务器「{server_code}」已停用。"

    try:
        result = await mcp_manager.call_tool(
            server, tool_name, args, timeout=float(server.timeout_sec or 30)
        )
    except Exception as exc:  # noqa: BLE001 - 外部边界，错误一律回灌给模型
        logger.warning("MCP 工具 %s 调用失败：%s", name, exc)
        return False, f"错误：MCP 工具调用失败（{type(exc).__name__}）：{exc}"

    ok, text = map_call_result(result, max_chars=settings.tool_result_max_chars)

    # 工具结果会**直接进 prompt**，它来自第三方服务器，可能含提示词注入或敏感
    # 内容。§20 的过滤点在「内容进 LLM 之前」，MCP 工具结果与用户输入位置
    # 一样、性质也一样——不能只过滤用户输入（§21.10 第 ④ 条）。
    _screen_tool_output(text, name)
    return ok, text


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
