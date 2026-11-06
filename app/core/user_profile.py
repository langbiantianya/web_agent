"""终端用户可读资料的规范化。"""

from __future__ import annotations

import re
import secrets

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_username(value: object) -> str | None:
    """trim + 小写并校验 username；空值或非法字符返回 ``None``。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _USERNAME_RE.fullmatch(normalized) else None


def generated_username() -> str:
    """生成不可从第三方资料推导的本地用户名。"""
    return f"user_{secrets.token_hex(6)}"


def normalize_email(value: object) -> str | None:
    """trim + 小写 email；空值与明显非法值转为 ``NULL``。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 320:
        return None
    return normalized if _EMAIL_RE.fullmatch(normalized) else None


def normalize_phone(value: object) -> str | None:
    """只接受已经符合 E.164 的号码，不猜测国家区号。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if _E164_RE.fullmatch(normalized) else None
