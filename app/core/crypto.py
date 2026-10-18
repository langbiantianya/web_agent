"""API Key 对称加解密（Fernet）。

``agent.api_key_cipher`` 存的是密文，加密密钥由 ``SETTINGS_SECRET_KEY`` 派生。

Fernet 每次加密使用随机 IV，因此**同一明文两次加密的密文不同**——这是刻意的：
密文不泄露「两个 Agent 用了同一个 key」这类信息。解密是确定性的。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config.settings import settings

_PREFIX = "fernet:"


def _fernet() -> Fernet:
    """按当前 SECRET_KEY 现造一个 Fernet 实例。

    Fernet 要求密钥恰好 32 字节，而 ``settings.secret_key`` 是任意长度、给人看
    的字符串，所以统一先 SHA-256 摘要成 32 字节再 urlsafe-b64 编码——这样
    SECRET_KEY 的长度和内容怎么改，调用方都不用动。

    刻意不做进程级缓存：Fernet 本身无状态（只持有一把密钥），重建开销可忽略，
    而每次现取能保证密钥轮换后立刻生效。
    """
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str | None) -> str:
    """加密。明文为空时返回空串（本地 vLLM 等无需 key 的场景）。"""
    if not plain:
        return ""
    return _PREFIX + _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(cipher: str | None) -> str:
    """解密。为空或格式非法时返回空串，交由调用方判定是否可用。"""
    if not cipher:
        return ""
    if not cipher.startswith(_PREFIX):
        # 兼容明文直存的存量数据
        return cipher
    try:
        return _fernet().decrypt(cipher[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # pragma: no cover - 密钥不匹配
        raise ValueError("api_key 解密失败：SECRET_KEY 与加密时不一致") from exc
