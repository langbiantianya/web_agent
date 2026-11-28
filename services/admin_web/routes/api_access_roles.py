"""Casbin 角色与权限矩阵的 JSON API —— 读侧（V4.10）。

与页面路由 ``routes/access_roles.py`` 共用
:func:`role_summary_rows` 的投影：角色、权限、继承、成员数与引用数都从
同一套 Casbin 规则算出，前端不需要自己推导。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from services.admin_web.access_api import permission_catalog
from services.admin_web.access_policy import (
    KNOWN_PERMISSIONS,
    permission_id,
    role_domains,
    role_summary_rows,
)
from services.admin_web.api_support import (
    api_route,
    authorize,
    json_response,
)
from services.admin_web.listing import parse_list_params
from services.admin_web.rbac import casbin_service
from services.admin_web.routes.access_roles import _mapping_counts
from services.admin_web.routes import api_access_roles_write

PAGE_CODE = "access_roles"
COMPONENT_CODE = "policy_list"


def register(app: Any) -> None:
    """把角色的 JSON API 挂到 app 上。"""
    api_access_roles_write.register(app)

    @app.get("/admin/api/access/roles")
    @api_route
    async def api_access_roles_list(request: Request):
        """列出角色、权限与继承关系。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            rules = await casbin_service.list_policies(
                db,
                tenant_id=(
                    None
                    if principal.is_platform_super_admin
                    else principal.tenant_id
                ),
            )
            summaries = role_summary_rows(
                rules, mapping_counts=await _mapping_counts(db, principal)
            )
            params = parse_list_params(
                request, allowed_sorts=("code", "domain")
            )
            keyword = params.q.casefold()
            items = [
                role
                for role in summaries
                if not keyword
                or keyword
                in " ".join(
                    (
                        role.code,
                        role.domain,
                        *(permission_id(item) for item in role.permissions),
                    )
                ).casefold()
            ]
            if params.sort == "code":
                items.sort(key=lambda role: role.code)
            elif params.sort == "domain":
                items.sort(key=lambda role: (role.domain, role.code))
            total = len(items)
            start = params.offset
            page_items = items[start:start + params.size]
            return json_response(
                {
                    "items": [
                        {
                            "code": role.code,
                            "domain": role.domain,
                            "permissions": [
                                permission_id(item) for item in role.permissions
                            ],
                            "parents": list(role.parents),
                            "memberCount": role.member_count,
                            "mappingCount": role.mapping_count,
                            "childCount": role.child_count,
                            "builtin": role.builtin,
                            "canDelete": role.can_delete,
                        }
                        for role in page_items
                    ],
                    "total": len(items),
                    "page": {
                        "q": params.q,
                        "sort": params.sort,
                        "page": params.page,
                        "size": params.size,
                    },
                    "permissionOptions": permission_catalog(sorted(KNOWN_PERMISSIONS)),
                    "domains": role_domains(summaries),
                    # 继承关系要挑「同域名下的其它角色」，这里给的是**全量**
                    # 角色编码（不受列表分页影响），前端直接拿它做候选。
                    "rolesByDomain": {
                        domain: sorted(
                            {
                                role.code
                                for role in summaries
                                if role.domain == domain
                            }
                        )
                        for domain in role_domains(summaries)
                    },
                }
            )
        finally:
            await db.close()
