"""请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# JSON 字段的深度与规模上限。
#
# 深度：pydantic 序列化一个 3000 层嵌套的 dict 会抛
# "Circular reference detected (depth exceeded)"，那是个**未捕获**的异常 →
# 500 Internal Server Error，违反「参数不合法 → 400」这条约定。
# 规模：一个几十万键的 dict 光是解析就要吃掉大量内存，而它最后只是被原样存进
# 一个 JSON 列。
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 500


def check_json_shape(
    value: Any, *, max_depth: int = MAX_JSON_DEPTH, max_items: int = MAX_JSON_ITEMS
) -> Any:
    """校验 JSON 字段的深度与规模，不合格就抛 ``ValueError``。

    抛 ``ValueError`` 而不是自定义异常：pydantic 会把它包成
    ``ValidationError``，由统一异常处理器转成 400——参数不合法就该是 400，
    不是 500。

    Args:
        value: 待校验的 JSON 值。
        max_depth: 允许的最大嵌套层数。
        max_items: 允许的最大元素总数（dict 的键数 + list 的长度，累加）。

    Returns:
        原样返回 ``value``（方便直接用在 validator 的返回值上）。

    Raises:
        ValueError: 嵌套太深或元素太多。
    """
    if value is None:
        return value
    items = 0

    def _walk(node: Any, depth: int) -> None:
        """递归统计元素数并检查深度。"""
        nonlocal items
        if depth > max_depth:
            raise ValueError(f"JSON 嵌套超过 {max_depth} 层")
        if isinstance(node, dict):
            items += len(node)
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, (list, tuple)):
            items += len(node)
            for v in node:
                _walk(v, depth + 1)

    _walk(value, 1)
    if items > max_items:
        raise ValueError(f"JSON 元素总数超过 {max_items} 个")
    return value


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PageMeta(BaseModel):
    total: int = 0
    limit: int = 50
    offset: int = 0


class HealthOut(BaseModel):
    status: str = "ok"
    app: str
    db: str
    # Redis 依赖状态：ok / error / disabled（未启用）。
    redis: str = "disabled"
    active_runs: int = 0
    version: str = ""


class ErrorOut(BaseModel):
    code: str
    message: str


Any_ = Any
