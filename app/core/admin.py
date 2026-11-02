"""管理员门禁（设计文档 §22.10）。

## 这道门为什么存在

技能正文会进 prompt，**模型会把它当指令执行**。所以「谁能写技能」≈「谁能改模型的
行为」≈「能让任意代码在 studio 容器里跑」。

技能进了数据库之后，「改它要改代码 + 走部署」这道物理阻力没有了——后台就能改。
阻力没了，权限就得从别处补回来，就是这个门。

## 为什么是共享密钥而不是用户体系

本项目目前**没有用户模型**（§1.1：单部署多 Agent，没有登录体系）。为了一个权限位
引入用户表、会话、RBAC 是另一个量级的工程，而且和「技能」这个功能无关。所以这里
用一把**共享密钥**：持有 ``ADMIN_KEY`` 的调用方即管理员。

将来真的有了用户体系，把 :func:`require_admin` 换成按用户查权限即可，调用点不用动。
"""

from __future__ import annotations

import secrets

from app.config.settings import settings
from app.core.errors import UnauthorizedError


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
