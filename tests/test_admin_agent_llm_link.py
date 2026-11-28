"""管理端 Agent 与 LLM 连接联动测试。"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.models.agent import Agent
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from services.admin_web import app as admin_web_app
from services.admin_web.accounts import (
    create_super_admin_account,
    create_super_admin_session,
)
from services.admin_web.db import AdminSessionLocal
from services.admin_web.notifications import DeliveryResult
from services.admin_web.registry import seed_registry
from services.admin_web.security import (
    super_csrf_cookie_name,
    super_session_cookie_name,
)


async def _password_sender(*, username, email, phone, password):
    """测试账号创建只需要成功回执，不发送真实通知。"""
    return DeliveryResult(email_status="sent", sms_status="skipped")


async def _logged_in_client() -> tuple[httpx.AsyncClient, str]:
    """建立 platform super admin 会话。"""
    tag = uuid.uuid4().hex[:10]
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        account, _ = await create_super_admin_account(
            db,
            username=f"admin_{tag}",
            email=f"admin-{tag}@example.com",
            phone=None,
            password_sender=_password_sender,
        )
        token, csrf = await create_super_admin_session(db, account)
        await db.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_web_app.app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    return client, csrf


