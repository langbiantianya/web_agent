"""管理员门禁（设计文档 §22.10）。

## 这道门为什么存在

技能正文会进 prompt，**模型会把它当指令执行**。所以「谁能写技能」≈「谁能改模型的
行为」≈「能让任意代码在 studio 容器里跑」。

技能进了数据库之后，「改它要改代码 + 走部署」这道物理阻力没有了——后台就能改。
阻力没了，权限就得从别处补回来，就是这个门。

## 两层身份

环境变量 ``ADMIN_KEY`` 只在首次部署和应急时使用，代表默认租户的引导管理员。
日常身份保存在 ``admin_user``：使用独立 API Key、支持角色、停用和轮换，数据库只保存
HMAC-SHA256 摘要。:func:`resolve_admin` 同时解析两类主体，路由再按角色授权。

:func:`require_admin` 保留为兼容旧调用点的引导 Key 校验；新代码应使用
:func:`resolve_admin` 获取主体，再通过 :func:`require_min_role` 检查权限。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.models.base import utcnow

ROLES = ("viewer", "operator", "admin")
_ROLE_RANK = {name: index for index, name in enumerate(ROLES)}


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    """认证后的管理员主体。"""

    username: str
    tenant_id: str
    role: str
    bootstrap: bool = False


def admin_key_hash(key: str) -> str:
    """用部署密钥对管理员 API Key 做 HMAC-SHA256。"""
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        (key or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def new_admin_key() -> str:
    """生成只在创建/轮换响应中出现一次的管理员 Key。"""
    return "wa_admin_" + secrets.token_urlsafe(32)


def require_min_role(principal: AdminPrincipal, minimum: str) -> AdminPrincipal:
    """校验主体至少具备指定角色。

    Raises:
        ForbiddenError: 角色不足。
    """
    if minimum not in _ROLE_RANK:
        raise ValueError(f"未知管理员角色：{minimum}")
    if _ROLE_RANK.get(principal.role, -1) < _ROLE_RANK[minimum]:
        raise ForbiddenError(
            f"当前角色 {principal.role} 不能执行该操作（至少需要 {minimum}）"
        )
    return principal


def admin_key_matches(key: str | None) -> bool:
    """密钥是否正确。**不抛异常**。

    给「需要判断而不是拒绝」的调用点用——比如请求体上限中间件：它要在**没有**
    管理员密钥时把放宽的上限撤掉，而不是回一个 401（那个 401 是路由层的活）。

    未配置 ``ADMIN_KEY`` 时一律返回 False：没有配置就没有管理员。

    Args:
        key: 调用方带上来的密钥。

    Returns:
        密钥正确返回 True。
    """
    configured = settings.admin_key
    if not configured or not key:
        return False
    return secrets.compare_digest(key, configured)


def require_admin(key: str | None) -> str:
    """校验管理员密钥。

    Args:
        key: 调用方带上来的密钥（``X-Admin-Key`` 头）。

    Returns:
        管理员标识，写进审计的 ``author``。

    Raises:
        UnauthorizedError: 未配置密钥、没带密钥、密钥不对——三种情况都拒。
    """
    configured = settings.admin_key
    if not configured:
        # fail-closed：没有配置就没有管理员。这道门后面是「改模型的行为」，
        # 默认开着等于没门。
        raise UnauthorizedError(
            "未配置 ADMIN_KEY，技能写接口已禁用（这是有意的：默认关闭）"
        )
    if not key:
        raise UnauthorizedError("缺少管理员密钥")
    # compare_digest：定长比较，避免按字符比较泄露前缀信息。
    if not secrets.compare_digest(key, configured):
        raise UnauthorizedError("管理员密钥不正确")
    return settings.admin_author


async def resolve_admin(db: AsyncSession, key: str | None) -> AdminPrincipal:
    """解析环境引导 Key 或数据库管理员 Key。

    环境引导 Key 始终拥有默认租户的 admin 角色；数据库用户的角色和租户由记录
    决定。用户认证成功后更新 ``last_used_at``，但不阻塞认证失败路径。
    """
    if admin_key_matches(key):
        return AdminPrincipal(
            username=settings.admin_author,
            tenant_id="default",
            role="admin",
            bootstrap=True,
        )

    if not key:
        raise UnauthorizedError("缺少管理员密钥")
    from app.models.admin_user import AdminUser

    key_digest = admin_key_hash(key)
    row = await db.scalar(
        sa.select(AdminUser).where(
            AdminUser.api_key_hash == key_digest,
            AdminUser.enabled == 1,
        )
    )
    if row is None:
        raise UnauthorizedError("管理员密钥不正确")
    row.last_used_at = utcnow()
    await db.commit()
    return AdminPrincipal(
        username=row.username,
        tenant_id=row.tenant_id,
        role=row.role,
    )
