"""admin 门禁（设计文档 §22.10 第 1 条）。"""

from __future__ import annotations

import types

import pytest

from app.core.admin import require_admin
from app.core.errors import UnauthorizedError


def _settings(admin_key: str, admin_author: str = "admin"):
    """造一个 settings 替身。"""
    return types.SimpleNamespace(admin_key=admin_key, admin_author=admin_author)


def test_admin_key_not_configured_is_closed(monkeypatch):
    """未配置 ADMIN_KEY 时一律拒——fail-closed。

    这道门后面是「谁能改模型的行为」。默认开着等于没有门。
    """
    monkeypatch.setattr("app.core.admin.settings", _settings(""))
    with pytest.raises(UnauthorizedError):
        require_admin(None)
    with pytest.raises(UnauthorizedError):
        require_admin("")


def test_admin_key_missing_rejected(monkeypatch):
    """没带密钥 → 拒。"""
    monkeypatch.setattr("app.core.admin.settings", _settings("secret-key"))
    with pytest.raises(UnauthorizedError):
        require_admin(None)


def test_admin_key_mismatch_rejected(monkeypatch):
    """密钥不对 → 拒。"""
    monkeypatch.setattr("app.core.admin.settings", _settings("secret-key"))
    with pytest.raises(UnauthorizedError):
        require_admin("wrong-key")


def test_admin_key_match_returns_author(monkeypatch):
    """密钥对上 → 返回调用方标识，供审计记 author。"""
    monkeypatch.setattr("app.core.admin.settings", _settings("secret-key"))
    assert require_admin("secret-key") == "admin"


def test_admin_author_is_configurable(monkeypatch):
    """审计里的调用方标识可配置——多密钥部署时能看出是谁改的。"""
    monkeypatch.setattr("app.core.admin.settings", _settings("k", "alice"))
    assert require_admin("k") == "alice"
