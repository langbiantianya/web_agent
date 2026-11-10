"""表单校验失败时的「保留输入 + 字段级错误」支撑。

UX-03 记录的问题：表单校验失败后一律重定向到一句通用的
「The submitted values are invalid.」，用户填的东西全部丢失，也看不出
到底哪个字段不对。

这里提供三条支撑：

1. :func:`field_errors` —— 把 pydantic 的校验错误摊平成
   ``{字段名: 文案}``，让页面能逐字段标注；
2. :func:`invalid_form_redirect` —— 校验失败时把**已填的值**与
   **字段级错误**一起带回原页面，而不是丢给一句通用 Flash；
3. :func:`take_invalid` —— 页面渲染时取回（并作废）那一份状态。

## 为什么不把值放进 URL

URL 会进浏览器历史、Referer 与服务器访问日志；而且表单里可能有
API Key 这类**只写**字段。已填值只放在**当前响应**里（服务端渲染的
页面），URL 上只带一个随机 nonce 用于把后续请求关联回来。

## nonce 的作用

表单提交失败后重定向回 ``?invalid=<nonce>``。nonce 只在**同一次
失败重定向**里有意义：页面渲染时用它去取回本次失败的值与错误，
用完即弃（一次性）。它不承载数据，因此泄漏了也没有额外风险。

## 只写字段不回显

密码类字段（``api_key`` / ``client_secret``）**不**进入保留值：
即使失败也不回显，避免明文密钥出现在页面或日志里。
"""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import ValidationError

# 表单里属于「只写」的字段：失败时也不回显，避免密钥出现在页面上。
WRITE_ONLY_FIELDS = frozenset({"api_key", "client_secret", "password"})

# 一次失败提交对应的输入与错误，进程内短期保存。
#
# 刻意**不落库**：这是渲染用的临时状态，进程重启后丢失没有副作用——
# 用户重新提交即可。落库反而要处理过期清理与租户隔离。
_INVALID_FORMS: dict[str, dict[str, Any]] = {}

# nonce 的最大保存条数。超出后丢最旧的，避免长期运行累积。
_MAX_INVALID_FORMS = 64


def field_errors(exc: ValidationError) -> dict[str, str]:
    """把 pydantic 校验错误摊平成 ``{字段名: 文案}``。

    只保留**第一条**错误：同一个字段报多条时，全列出来反而更难读。
    字段名取定位元组的最后一段——嵌套结构的字段（如
    ``extra_params`` 内部）统一归到顶层字段上。
    """
    errors: dict[str, str] = {}
    for item in exc.errors():
        location = item.get("loc") or ()
        if not location:
            continue
        name = str(location[-1])
        if name in errors:
            continue
        errors[name] = str(item.get("msg") or "Invalid value.")
    return errors


def keep_values(values: dict[str, Any]) -> dict[str, Any]:
    """去掉只写字段，只留下可以安全回显的输入。"""
    return {
        key: value
        for key, value in values.items()
        if key not in WRITE_ONLY_FIELDS
    }


def remember_invalid(
    values: dict[str, Any],
    errors: dict[str, str],
) -> str:
    """保存一次失败提交，返回可在 URL 上携带的 nonce。"""
    nonce = secrets.token_urlsafe(12)
    _INVALID_FORMS[nonce] = {
        "values": keep_values(values),
        "errors": errors,
    }
    if len(_INVALID_FORMS) > _MAX_INVALID_FORMS:
        for key in list(_INVALID_FORMS)[
            : len(_INVALID_FORMS) - _MAX_INVALID_FORMS
        ]:
            _INVALID_FORMS.pop(key, None)
    return nonce


def take_invalid(nonce: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """取回（并作废）一次失败提交的值与错误。

    取过即弃：同一个 nonce 不能反复渲染出同一份带用户输入的页面。
    """
    if not nonce:
        return {}, {}
    entry = _INVALID_FORMS.pop(nonce, None)
    if entry is None:
        return {}, {}
    return dict(entry.get("values") or {}), dict(entry.get("errors") or {})


def invalid_form_redirect(
    path: str,
    values: dict[str, Any],
    errors: dict[str, str],
) -> str:
    """构造带 ``?invalid=<nonce>`` 的重定向目标。"""
    nonce = remember_invalid(values, errors)
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}invalid={nonce}"


__all__ = [
    "WRITE_ONLY_FIELDS",
    "field_errors",
    "invalid_form_redirect",
    "keep_values",
    "remember_invalid",
    "take_invalid",
]
