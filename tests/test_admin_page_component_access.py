"""页面与组件权限矩阵的保存、覆盖和重置测试。"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.models.admin_control import (
    AdminComponentPermission,
    AdminPagePermission,
)
from services.admin_web.db import AdminSessionLocal
from tests.test_admin_complete import _logged_in_client


