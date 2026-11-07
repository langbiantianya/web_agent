"""admin-web 兼容入口，Casbin 实现位于公共 core 层。"""

from app.core.admin_authz import (
    CASBIN_MODEL,
    DEFAULT_POLICIES,
    ROLE_IMPLICATION,
    CasbinService,
    RoleMappingRow,
    casbin_service,
)

__all__ = [
    "CASBIN_MODEL",
    "DEFAULT_POLICIES",
    "ROLE_IMPLICATION",
    "CasbinService",
    "RoleMappingRow",
    "casbin_service",
]
