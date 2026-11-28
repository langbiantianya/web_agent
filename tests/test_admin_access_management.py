"""后台角色、权限矩阵和 OAuth 映射的可运营性测试。"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.db import AdminSessionLocal
from services.admin_web.rbac import casbin_service
from tests.test_admin_complete import _logged_in_client

