"""退役的旧管理员 API Key 接口。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.errors import GoneError

router = APIRouter(prefix="/api/admin-users", tags=["admin-users"])


def _gone() -> None:
    """统一返回稳定 410，提示改用独立 admin-web。"""
    raise GoneError("旧管理员 API Key 已退役，请使用独立 admin-web")


@router.api_route("", methods=["GET", "POST"])
async def retired_admin_users_root() -> None:
    """旧管理员列表与创建入口。"""
    _gone()


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def retired_admin_users_path(path: str) -> None:
    """旧管理员详情、更新、删除和 Key 轮换入口。"""
    _gone()
