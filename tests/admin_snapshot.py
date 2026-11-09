"""管理端页面的渲染快照工具。

比对的是 **Python 元素树** 而不是 HTML 文本：Ludic 渲染出的就是元素对象，
在 Python 层比对比解析 HTML 字符串精确得多，而且不需要任何第三方解析库。

结构等价的定义：
- 属性**顺序**变化不算差异（比对前排序）；
- 标签、属性值、文本、children 结构变化算差异；
- class 名变化**算**差异——这是「不改 class 名」的技术兜底。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots" / "admin"

# CSRF token 与时间戳每次渲染都不同，必须归一化，否则快照必然失败。
CSRF_PLACEHOLDER = "<CSRF>"
TIMESTAMP_PLACEHOLDER = "<TS>"

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?")


def _normalize_text(text: str) -> str:
    """把文本里的时间戳换成占位符。"""
    return _TIMESTAMP_RE.sub(TIMESTAMP_PLACEHOLDER, text)


def _normalize_value(key: str, value: Any) -> Any:
    """按属性名与值类型归一化。"""
    if key == "csrf_token":
        return CSRF_PLACEHOLDER
    if isinstance(value, str):
        return _normalize_text(value)
    return value


def normalize(node: Any) -> Any:
    """把元素（或字符串子节点）转成可 JSON 序列化的结构。

    标签名取 ``html_name``；属性取 ``aliased_attrs``（HTML 名，Python 侧的
    ``class_`` 在那里是 ``class``）并排序；``children`` 是元素与字符串的
    混排元组，分别处理。
    """
    if isinstance(node, str):
        return {"text": _normalize_text(node)}
    attrs = {
        key: _normalize_value(key, value)
        for key, value in sorted(getattr(node, "aliased_attrs", {}).items())
    }
    return {
        "tag": getattr(node, "html_name", type(node).__name__),
        "attrs": attrs,
        "children": [normalize(child) for child in getattr(node, "children", ())],
    }


def route_table(app: Any) -> list[dict]:
    """应用的路由表：method + path + **顺序**。

    顺序是行为的一部分（Starlette 首个匹配 path 的路由胜出），所以这里
    保序而不是排序。
    """
    table: list[dict] = []
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = sorted(getattr(route, "methods", None) or [])
        table.append({"path": path, "methods": methods})
    return table


def write_snapshot(path: Path, payload: Any) -> None:
    """写快照文件（缩进 + 排序键，便于 review diff）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_snapshot(path: Path) -> Any:
    """读快照文件。"""
    return json.loads(path.read_text(encoding="utf-8"))
