"""OAuth Provider 表单的解析。

管理端 Provider 表单要同时喂给「创建」与「更新」两条路径，字段解析只
实现一次；校验失败时用到的可回显值也在这里收集（密钥除外）。
"""

from __future__ import annotations

from app.schemas.oauth_provider import OAuthProviderCreate
from services.admin_web.common import as_bool, csv_list


def _provider_form_values(form) -> dict:
    """解析管理端 OAuth Provider 表单中的非密文字段。"""
    return {
        "display_name": str(form.get("display_name") or ""),
        "issuer": str(form.get("issuer") or ""),
        "discovery_url": str(form.get("discovery_url") or "") or None,
        "authorization_endpoint": (
            str(form.get("authorization_endpoint") or "") or None
        ),
        "token_endpoint": str(form.get("token_endpoint") or "") or None,
        "jwks_uri": str(form.get("jwks_uri") or "") or None,
        "client_id": str(form.get("client_id") or ""),
        "token_auth_method": str(
            form.get("token_auth_method") or "client_secret_basic"
        ),
        "scopes": csv_list(form.get("scopes")),
        "username_claim": str(
            form.get("username_claim") or "preferred_username"
        ),
        "role_claim": str(form.get("role_claim") or "groups"),
        "role_claim_required": as_bool(
            form.get("role_claim_required"), True
        ),
        "role_sync_max_age_seconds": int(
            str(form.get("role_sync_max_age_seconds") or "1800")
        ),
        "auto_create": as_bool(form.get("auto_create"), True),
        "client_login_enabled": as_bool(
            form.get("client_login_enabled"), True
        ),
        "admin_login_enabled": as_bool(
            form.get("admin_login_enabled"), True
        ),
        "enabled": as_bool(form.get("enabled"), True),
    }


def _submitted_values(form) -> dict:
    """收集表单里可以安全回显的值（密钥不回显）。"""
    return {
        key: str(form.get(key) or "")
        for key in (
            "code",
            "display_name",
            "issuer",
            "client_id",
            "discovery_url",
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
            "token_auth_method",
            "scopes",
            "username_claim",
            "role_claim",
            "role_sync_max_age_seconds",
            "client_login_enabled",
            "admin_login_enabled",
            "role_claim_required",
            "auto_create",
            "enabled",
        )
    }
