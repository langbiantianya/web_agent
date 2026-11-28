"""本轮 P0/P1 修复的验收用例。

覆盖 2026-11-10 UX 评审里 P0/P1 各项的**可观察行为**：

- UX-01：删除要先过确认对话框，并且可以撤销；
- UX-03：校验失败保留输入并逐字段标错；
- UX-05：在用的连接显示引用数量并锁定危险操作；
- UX-06：列表支持搜索 / 排序 / 分页，且状态可分享（落在 URL 上）；
- UX-07：探测状态是可被读屏播报的活动区域；
- UX-09：编辑表单进独立面板，可用 ``?resource=`` 深链接；
- UX-19/UX-20：能力选择器有搜索、折叠、计数与建议差异。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models.agent import Agent
from app.models.admin_deleted_resource import AdminDeletedResource
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.skill_repo import skill_repo
from services.admin_web.listing import parse_list_params


async def _logged_in_client():
    from tests.test_admin_web import _create_seeded_logged_in_super_admin

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    return client, csrf


async def _seed_endpoint(db, code: str, name: str) -> None:
    await llm_endpoint_repo.create(
        db,
        code=code,
        name=name,
        base_url="https://ux.example/v1",
        model="ux-model",
        api_key_cipher="cipher",
        priority=10,
        enabled=1,
    )


def test_list_params_tolerate_dirty_query():
    """UX-06：URL 里的脏值不能让页面 500，也不该被当成有效值。"""
    class _FakeRequest:
        def __init__(self, params):
            self.query_params = params

    params = parse_list_params(
        _FakeRequest({"q": " a ", "sort": "unknown", "page": "abc", "size": "-5"}),
        allowed_sorts=("name",),
    )
    assert params.q == "a"
    assert params.sort == ""
    assert params.page == 1
    assert params.size == 20


