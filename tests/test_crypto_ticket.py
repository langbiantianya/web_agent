"""API Key 加密与 SSE ticket。"""

from __future__ import annotations

import time

import pytest

from app.core.crypto import decrypt, encrypt
from app.core.errors import UnauthorizedError
from app.core.ticket import issue_ticket, verify_ticket


def test_encrypt_decrypt_roundtrip():
    """密文不能等于明文、不能残留明文字样，且能原样解回。

    防的是「压根没加密直接入库」和「加密了但换了算法解不回来」两类事故。
    """
    key = "sk-真实密钥-12345"
    cipher = encrypt(key)
    assert cipher != key
    assert "真实密钥" not in cipher
    assert decrypt(cipher) == key


def test_encrypt_uses_random_iv():
    """Fernet 随机 IV：两次加密密文不同，但都能解回同一个明文。

    这比确定性加密更安全——密文不会泄露「两个 Agent 共用同一个 key」。
    """
    a, b = encrypt("sk-x"), encrypt("sk-x")
    assert a != b
    assert decrypt(a) == decrypt(b) == "sk-x"


def test_empty_key_roundtrip():
    """没配 key 的 Agent（本地 vLLM）不能崩在加解密上，空值要能原样穿透。"""
    assert encrypt("") == ""
    assert decrypt("") == ""
    assert decrypt(None) == ""


def test_decrypt_rejects_tampered_ciphertext():
    """密文被改一个字节就必须报错，不能悄悄解出「看起来像明文」的东西。"""
    cipher = encrypt("sk-secret")
    with pytest.raises(ValueError):
        decrypt(cipher[:-4] + "AAAA")


def test_plaintext_passthrough_for_legacy_rows():
    """存量明文数据直接返回，不报错。"""
    assert decrypt("sk-plain") == "sk-plain"


def test_ticket_roundtrip():
    """签发的 ticket 能验回 uid/run_id，且 TTL 必须是合理的正值。

    TTL 漏发或发成 0 会让所有 SSE 连接瞬时失效。
    """
    ticket, ttl = issue_ticket("u1", run_id=42)
    payload = verify_ticket(ticket)
    assert payload["uid"] == "u1"
    assert payload["run_id"] == 42
    assert 0 < ttl <= 3600


def test_ticket_bound_to_run_rejects_other_run():
    """凭证与 run_id 绑定：一个 run 的 ticket 不能拿去订阅别人的流。"""
    ticket, _ = issue_ticket("u1", run_id=42)
    verify_ticket(ticket, run_id=42)  # 匹配
    with pytest.raises(UnauthorizedError, match="不匹配"):
        verify_ticket(ticket, run_id=43)


def test_unbound_ticket_works_for_any_run():
    """不绑 run_id 的 ticket（如整页级连接）应能用于任意 run。"""
    ticket, _ = issue_ticket("u1")
    verify_ticket(ticket, run_id=99)


def test_expired_ticket_rejected():
    """过期凭证必须拒：否则一条断过的老连接可以在过期后继续偷看输出。"""
    ticket, _ = issue_ticket("u1", ttl=-1)
    with pytest.raises(UnauthorizedError, match="过期"):
        verify_ticket(ticket)


def test_garbage_ticket_rejected():
    """任意乱码不能被当成合法凭证——防的是「只解出来不验签」的懒实现。"""
    with pytest.raises(UnauthorizedError):
        verify_ticket("not-a-real-ticket")


def test_empty_ticket_rejected():
    """缺 ticket 时同样拒绝，不能退化成「匿名也能订阅」。"""
    with pytest.raises(UnauthorizedError):
        verify_ticket("")


def test_ticket_cannot_be_forged():
    """篡改过期时间后签名校验失败。"""
    ticket, _ = issue_ticket("u1", ttl=60)
    with pytest.raises(UnauthorizedError):
        verify_ticket(ticket[:-2] + ("aa" if not ticket.endswith("aa") else "bb"))
