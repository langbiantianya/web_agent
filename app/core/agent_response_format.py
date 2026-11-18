"""Agent 输出格式 —— JSON 响应的指令生成与模型输出解析。

**为什么这两件事必须放在同一个模块里**：

1. 编排器（拼 system 消息发给模型）与提示词预览接口（给人看的「实际生效的
   提示词」）必须发出**逐字一致**的指令。两处各写一遍的话，预览迟早会跟真正
   发出去的内容不一致——而「后台看到的 = 模型收到的」是这个项目的硬约定。
2. 解析规则（代码围栏、空行、非法 JSON 的措辞）只有一份实现，「Agent 配了
   JSON 就一定拿得到 JSON」才是个能测的契约，而不是各处口头约定。

**范围**：这里只做**提示词层**的约束与服务端校验，不去改 LLM 请求参数。
OpenAI 兼容协议里的 ``response_format={"type": "json_object"}`` 并非所有
端点都认（MiniMax / 部分自建端点会直接 400），所以它仍由使用方通过
``extra_params`` 自行选择；本模块提供的是**不依赖端点能力**的那一层。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 合法的输出格式取值。只有这两种：文本与 JSON。
RESPONSE_FORMAT_TEXT = "text"
RESPONSE_FORMAT_JSON = "json"
RESPONSE_FORMATS: tuple[str, ...] = (RESPONSE_FORMAT_TEXT, RESPONSE_FORMAT_JSON)

# 缺省输出格式。存量 Agent 没有这一列，语义就是「照常输出文本」。
DEFAULT_RESPONSE_FORMAT = RESPONSE_FORMAT_TEXT

# 模型偶尔会把 JSON 包在 Markdown 代码围栏里。整段匹配围栏（含语言标注）。
_FENCE_RE = re.compile(
    r"^```[A-Za-z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```[ \t]*$",
    re.DOTALL,
)


def normalize_response_format(value: Any) -> str:
    """把任意输入归一成合法的输出格式。

    不认识的一律按 ``text`` 处理：输出格式是**提示词层面的约束**，脏值不该
    让整个 Agent 配置读不出来。管理端的写入路径另有严格校验（见
    :func:`parse_response_format`），这里只负责读。

    Args:
        value: 库里读到的原始值（可能是 ``None``、空串或历史脏值）。

    Returns:
        ``"text"`` 或 ``"json"``。
    """
    text = str(value or "").strip().lower()
    return RESPONSE_FORMAT_JSON if text == RESPONSE_FORMAT_JSON else RESPONSE_FORMAT_TEXT


def parse_response_format(value: Any) -> str:
    """严格解析输出格式：非法值直接抛错。

    与 :func:`normalize_response_format` 的区别是**读写方向不同**：写路径上
    静默回落会把用户的笔误变成「配了但没生效」，所以这里必须报错，让表单
    标到具体字段上。

    Args:
        value: 表单或接口传入的原始值。

    Returns:
        ``"text"`` 或 ``"json"``。

    Raises:
        ValueError: 取值不在白名单里（空值按缺省 ``text`` 处理）。
    """
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_RESPONSE_FORMAT
    if text not in RESPONSE_FORMATS:
        raise ValueError("invalid_response_format")
    return text


def is_json_response(response_format: Any) -> bool:
    """该 Agent 是否要求 JSON 输出。

    Args:
        response_format: Agent 上的输出格式字段。

    Returns:
        True 表示要求 JSON。
    """
    return normalize_response_format(response_format) == RESPONSE_FORMAT_JSON


def json_instruction(response_schema: dict | None = None) -> str:
    """生成追加到 system 消息里的 JSON 输出指令。

    Args:
        response_schema: 可选的 JSON Schema（对象）。给了就把结构要求写进去，
            没给就只要求「顶层是 JSON 对象」。

    Returns:
        指令正文。
    """
    lines = [
        "【输出格式】本次回答必须且只能是一个 JSON 值。",
        "硬性要求：",
        "1. 不要输出任何解释、前言、结语或 Markdown 代码围栏（```）。",
        "2. JSON 前后不要有多余文字——服务端会直接解析你的整段输出，"
        "多一个字都会导致解析失败。",
    ]
    if response_schema:
        lines.append("3. JSON 必须满足下面这个 JSON Schema：")
        lines.append(
            json.dumps(response_schema, ensure_ascii=False, indent=2, sort_keys=True)
        )
    else:
        lines.append("3. 顶层必须是一个 JSON 对象（形如 {\"key\": \"value\"}）。")
    return "\n".join(lines)


def build_response_format_messages(
    response_format: Any,
    response_schema: dict | None = None,
) -> list[dict]:
    """构造要追加进 system 消息簇的消息。

    文本模式返回空列表——**不给 message 加任何东西**。多插一条
    「输出格式说明」会让提示词预览、token 预算与实际调用三处对不上，而它
    对文本输出又毫无用处。

    Args:
        response_format: Agent 上的输出格式字段。
        response_schema: 可选的 JSON Schema。

    Returns:
        消息列表（空或一条 ``role=system`` 的消息）。
    """
    if not is_json_response(response_format):
        return []
    return [
        {"role": "system", "content": json_instruction(response_schema)}
    ]


def strip_code_fence(text: str) -> str:
    """剥掉包住整段内容的 Markdown 代码围栏。

    Args:
        text: 原始文本。

    Returns:
        去掉围栏后的正文；没有围栏时原样返回。
    """
    match = _FENCE_RE.match(text.strip())
    if match is None:
        return text.strip()
    return match.group("body").strip()


def _json_candidates(text: str) -> list[str]:
    """列出可能承载 JSON 的候选片段，按「越靠后越可能是结论」排序。

    模型常在 JSON 前面加一句「好的，结果如下：」。整段不是 JSON 时，把
    围栏里的内容也拿来试一遍——**只试围栏**，不做「从散文里截一段 JSON」
    这种猜测：截出来的片段多半是半截 JSON，解析出来是错的数据，比失败更糟。

    Args:
        text: 原始文本。

    Returns:
        候选字符串列表（不重复）。
    """
    candidates: list[str] = [text.strip()]
    blocks = re.findall(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?```", text, re.DOTALL)
    for block in reversed(blocks):
        candidate = block.strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def parse_json_response(text: str) -> tuple[Any, str]:
    """解析模型的 JSON 输出。

    Args:
        text: 模型的整段输出。

    Returns:
        ``(解析结果, 错误说明)``。解析成功时错误说明是空串，解析结果可能是
        任何 JSON 值（对象、数组、字符串、数字）；失败时结果是 ``None``，
        错误说明是一句能直接给用户看的话。
    """
    raw = (text or "").strip()
    if not raw:
        return None, "模型没有返回任何内容"
    last_error = ""
    for candidate in _json_candidates(raw):
        try:
            return json.loads(candidate), ""
        except json.JSONDecodeError as exc:
            if not last_error:
                last_error = f"不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
    return None, last_error or "不是合法 JSON"


def parse_response_schema(raw: Any) -> dict | None:
    """把表单里的 JSON Schema 文本解析成对象。

    Args:
        raw: 表单文本。空串 / ``None`` 表示「不限定结构」。

    Returns:
        解析出的 schema 对象；不限定结构时返回 ``None``。

    Raises:
        ValueError: 文本不是合法 JSON，或解析出来不是对象。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_response_schema") from exc
    if not isinstance(parsed, dict):
        raise ValueError("invalid_response_schema")
    return parsed
