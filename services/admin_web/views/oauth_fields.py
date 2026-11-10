"""OAuth Provider 表单的字段声明。

从 ``oauth_providers.py`` 拆出来：字段声明本身就不短，再和页面渲染挤在
一起会突破单文件行数上限。
"""

from __future__ import annotations

from services.admin_web.views.fields import Field


_YES_NO = (("Yes", "1"), ("No", "0"))
_ENABLED_DISABLED = (("Enabled", "1"), ("Disabled", "0"))

PROVIDER_FIELDS = (
    Field(
        "display_name",
        "Display name",
        required=True,
    ),
    Field(
        "issuer",
        "Issuer",
        kind="url",
        required=True,
        placeholder="https://id.example.com",
    ),
    Field(
        "client_id",
        "Client ID",
        required=True,
    ),
    Field(
        "client_secret",
        "Client secret",
        kind="password",
        autocomplete="new-password",
        placeholder="Optional for public clients",
        edit_placeholder="Leave blank to keep current secret",
    ),
    Field("discovery_url", "Discovery URL", kind="url"),
    Field("authorization_endpoint", "Authorization endpoint", kind="url"),
    Field("token_endpoint", "Token endpoint", kind="url"),
    Field("jwks_uri", "JWKS URI", kind="url"),
    Field(
        "token_auth_method",
        "Token auth method",
        kind="select",
        options=(
            ("Client secret basic", "client_secret_basic"),
            ("Client secret post", "client_secret_post"),
        ),
    ),
    Field(
        "scopes",
        "Scopes",
        placeholder="openid profile email phone",
    ),
    Field(
        "username_claim",
        "Username claim",
        placeholder="preferred_username",
    ),
    Field(
        "role_claim",
        "Role claim",
        placeholder="groups",
    ),
    Field(
        "role_sync_max_age_seconds",
        "Role sync max age (seconds)",
        kind="number",
        min_value="1",
    ),
    Field(
        "client_login_enabled",
        "Client login",
        kind="select",
        options=_ENABLED_DISABLED,
    ),
    Field(
        "admin_login_enabled",
        "Admin login",
        kind="select",
        options=_ENABLED_DISABLED,
    ),
    Field(
        "role_claim_required",
        "Role claim required",
        kind="select",
        options=_YES_NO,
    ),
    Field(
        "auto_create",
        "Auto create client users",
        kind="select",
        options=_YES_NO,
    ),
    Field(
        "enabled",
        "Provider enabled",
        kind="select",
        options=_YES_NO,
    ),
)

PROVIDER_CREATE_FIELDS = (
    Field("code", "Provider code", required=True, modes=("create",)),
    *PROVIDER_FIELDS,
)

# 清空密钥是编辑态独有的动作：它把已存的密钥删掉，因此单独一个字段。
PROVIDER_EDIT_FIELDS = (
    *PROVIDER_FIELDS,
    Field(
        "clear_client_secret",
        "Clear existing client secret",
        kind="select",
        options=_YES_NO,
        modes=("edit",),
    ),
)
