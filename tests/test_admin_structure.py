"""管理端包结构契约。

重构的目标是「按业务域拆成两个包」，这个测试把目标本身钉住：
模块清单、导出清单、单文件行数上限，都不允许在重构中途悄悄偏离。
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "services" / "admin_web"

VIEW_MODULES = (
    "shell",
    "components",
    "fields",
    "login",
    "dashboard",
    "workflows",
    "agents",
    "skills",
    "mcp",
    "llm",
    "audit",
    "oauth_providers",
    "access",
    "roles",
    "platform_admins",
    "preferences",
)

ROUTE_MODULES = (
    "auth",
    "dashboard",
    "workflows",
    "agents",
    "skills",
    "mcp",
    "llm",
    "audit",
    "oauth_providers",
    "access",
    "platform_admins",
    "preferences",
)

# 单文件行数上限。设计文档的目标是「任何单文件不超过 350 行」。
MAX_MODULE_LINES = 350
MAX_APP_LINES = 200


@pytest.mark.parametrize("name", VIEW_MODULES)
def test_views_module_exists(name: str) -> None:
    """视图包下每个业务域都要有独立模块。"""
    assert (ADMIN / "views" / f"{name}.py").is_file()


@pytest.mark.parametrize("name", ROUTE_MODULES)
def test_route_module_exists(name: str) -> None:
    """路由包下每个业务域都要有独立模块。"""
    assert (ADMIN / "routes" / f"{name}.py").is_file()


def test_legacy_views_module_is_gone() -> None:
    """过渡期的 views_legacy.py 必须被删掉——留着它就等于没拆完。"""
    assert not (ADMIN / "views_legacy.py").exists()


def test_views_package_still_exports_every_page() -> None:
    """改名让位期间，视图包必须仍然导出全部页面函数。

    拆包是**逐步**进行的：中途任何一步都不能让
    ``from services.admin_web.views import X`` 失效——那会让整个管理端
    起不来，43 个既有测试当场全红。
    """
    import services.admin_web.views as views

    for name in (
        "login_page",
        "dashboard",
        "workflows_page",
        "workflow_editor_page",
        "agents_page",
        "skills_page",
        "mcp_page",
        "llm_page",
        "audit_page",
        "access_pages_page",
        "access_components_page",
        "roles_page",
        "mappings_page",
        "oauth_providers_page",
        "platform_admins_page",
        "preferences_page",
        "error_page",
        "shell",
    ):
        assert callable(getattr(views, name, None)), name


@pytest.mark.parametrize(
    "path",
    [
        *(ADMIN / "views" / f"{name}.py" for name in VIEW_MODULES),
        *(ADMIN / "routes" / f"{name}.py" for name in ROUTE_MODULES),
    ],
)
def test_module_stays_small(path: Path) -> None:
    """任何单文件不得超过行数上限。"""
    lines = len(path.read_text(encoding="utf-8").splitlines())
    assert lines <= MAX_MODULE_LINES, f"{path.name} 有 {lines} 行，超出上限"


def test_app_module_is_small() -> None:
    """app.py 只剩应用装配。"""
    lines = len((ADMIN / "app.py").read_text(encoding="utf-8").splitlines())
    assert lines <= MAX_APP_LINES, f"app.py 有 {lines} 行，超出上限"


def test_routes_expose_register() -> None:
    """每个路由模块都要暴露 register(app)。"""
    for name in ROUTE_MODULES:
        module = __import__(
            f"services.admin_web.routes.{name}", fromlist=["register"]
        )
        assert callable(getattr(module, "register", None)), name


def test_register_all_is_callable() -> None:
    """routes 包导出 register_all。"""
    module = __import__("services.admin_web.routes", fromlist=["register_all"])
    assert callable(getattr(module, "register_all", None))


def test_common_module_exposes_shared_helpers() -> None:
    """跨域辅助集中在 common.py，且不再带下划线前缀。"""
    module = __import__("services.admin_web.common", fromlist=["origin_ok"])
    for name in (
        "origin_ok",
        "safe_next",
        "callback_uri",
        "append_query",
        "principal_of",
        "preference_of",
        "authenticated",
        "with_auth",
        "require_csrf",
        "login_redirect",
        "unauthorized",
        "not_found",
        "html_error",
        "tenant_id_of",
        "flash_from_query",
        "as_bool",
        "csv_list",
        "json_object",
        "can_edit_resource",
        "operation_authorized",
        "business_redirect",
    ):
        assert callable(getattr(module, name, None)), name


def test_route_modules_never_import_the_parent_app() -> None:
    """域模块不许反向 import 应用模块——那会形成循环导入。"""
    pattern = re.compile(r"^\s*(?:from|import)\s+services\.admin_web\.app\b", re.M)
    for path in sorted((ADMIN / "routes").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not pattern.search(source), path.name


def test_app_module_has_no_inline_route_handlers() -> None:
    """app.py 只做装配：路由一律由 routes 包注册。"""
    source = (ADMIN / "app.py").read_text(encoding="utf-8")
    assert "@app.get(" not in source
    assert "@app.post(" not in source
