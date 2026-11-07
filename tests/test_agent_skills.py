"""Agent 绑定技能（设计文档 §22.5）。"""

from __future__ import annotations

import itertools

import httpx
import pytest

from app.core.skill_names import (
    SkillConfigError,
    known_skill_codes,
    normalize_skill_codes,
)
from app.main import create_app
from tests.conftest import authenticate_admin
from app.repo.skill_repo import skill_repo


def test_normalize_rejects_unknown_code():
    """未注册的技能 code 保存期就拒。"""
    with pytest.raises(SkillConfigError):
        normalize_skill_codes(["nope"], known={"demo"})


def test_normalize_accepts_known():
    """已注册的 code 通过，去重。"""
    assert normalize_skill_codes(["demo", "demo"], known={"demo"}) == ["demo"]


def test_normalize_none():
    """None 表示不用技能。"""
    assert normalize_skill_codes(None, known={"demo"}) is None


def test_normalize_empty_list_is_none():
    """空列表也归一成 None——「没绑技能」只有一种表示。"""
    assert normalize_skill_codes([], known={"demo"}) is None
    assert normalize_skill_codes([""], known={"demo"}) is None


def test_normalize_accepts_single_string():
    """单个字符串也接受——后台表单可能给的是字符串。"""
    assert normalize_skill_codes("demo", known={"demo"}) == ["demo"]


def test_normalize_rejects_non_string_item():
    """skills 里只能放 code 字符串，不能放函数定义之类。"""
    with pytest.raises(SkillConfigError):
        normalize_skill_codes([{"name": "x"}], known={"demo"})


def test_normalize_rejects_non_list():
    """形状不对直接拒。"""
    with pytest.raises(SkillConfigError):
        normalize_skill_codes(123, known={"demo"})


def test_known_codes_is_forgiving():
    """读路径宽容：未知 code 丢掉，不能让列表接口 500。"""
    assert known_skill_codes(["demo", "gone"], known={"demo"}) == ["demo"]
    assert known_skill_codes("not-a-list") is None


def test_known_codes_without_known_set_only_checks_shape():
    """拿不到库里的全量集合时，只做形状检查。"""
    assert known_skill_codes(["demo", "gone"]) == ["demo", "gone"]
    assert known_skill_codes([1, 2]) is None


def test_known_codes_none_when_empty():
    """空列表返回 None。"""
    assert known_skill_codes([]) is None
    assert known_skill_codes(None) is None


def test_known_codes_keeps_order_and_dedups():
    """保留原有顺序并去重。"""
    assert known_skill_codes(["b", "a", "b"]) == ["b", "a"]


# --------------------------------------------------------------- API 层


_counter = itertools.count()


@pytest.fixture
async def client():
    """真实 app + HTTP 客户端。

    手动走 lifespan：后台组件都在 startup 里注册。ASGITransport 对普通 JSON
    接口够用（这里不涉及 SSE 流式响应）。
    """
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            await authenticate_admin(c)
            yield c


def _agent_payload(code: str, skills=None) -> dict:
    """一份能建出 Agent 的最小请求体。"""
    payload = {
        "code": code,
        "name": code,
        "system_prompt": "你是助手",
        "model": "m",
        "base_url": "http://x",
    }
    if skills is not None:
        payload["skills"] = skills
    return payload


async def test_create_agent_rejects_unknown_skill(client, db):
    """绑一个不存在的技能 → 400。配置根本进不了库。"""
    await skill_repo.create(
        db, code="known", name="known", description="d", body="b", content_hash="h"
    )
    r = await client.post(
        "/api/agents", json=_agent_payload("ag_bad", skills=["unknown_skill"])
    )
    assert r.status_code == 400, r.text
    assert "未知技能" in r.json()["message"]


async def test_create_agent_binds_skill(client, db):
    """绑已存在的技能 → 存进去也读得回来。"""
    await skill_repo.create(
        db,
        code="bindable",
        name="bindable",
        description="d",
        body="b",
        content_hash="h",
    )
    r = await client.post(
        "/api/agents", json=_agent_payload("ag_ok", skills=["bindable"])
    )
    assert r.status_code == 201, r.text
    assert r.json()["skills"] == ["bindable"]

    back = await client.get("/api/agents/ag_ok")
    assert back.json()["skills"] == ["bindable"]


async def test_update_agent_can_unbind_all(client, db):
    """传空列表能解绑全部——「不传」与「清空」是两件事。"""
    await skill_repo.create(
        db,
        code="unbind",
        name="unbind",
        description="d",
        body="b",
        content_hash="h",
    )
    created = await client.post(
        "/api/agents", json=_agent_payload("ag_unbind", skills=["unbind"])
    )
    assert created.status_code == 201, created.text

    updated = await client.put("/api/agents/ag_unbind", json={"skills": []})
    assert updated.status_code == 200, updated.text
    assert not updated.json()["skills"]


async def test_disabled_skill_cannot_be_bound(client, db):
    """停用中的技能绑不上——绑了也不会生效，不如当场拒。"""
    await skill_repo.create(
        db,
        code="off_skill",
        name="off_skill",
        description="d",
        body="b",
        content_hash="h",
        enabled=0,
    )
    r = await client.post(
        "/api/agents", json=_agent_payload("ag_off", skills=["off_skill"])
    )
    assert r.status_code == 400, r.text


async def test_deleted_skill_disappears_from_agent_output(client, db):
    """技能被删掉后，Agent 上残留的绑定不该继续显示成一个空壳。"""
    code = f"gone_{next(_counter)}"
    await skill_repo.create(
        db, code=code, name=code, description="d", body="b", content_hash="h"
    )
    created = await client.post(
        "/api/agents", json=_agent_payload("ag_gone", skills=[code])
    )
    assert created.status_code == 201, created.text
    assert created.json()["skills"] == [code]

    await skill_repo.delete(db, code)
    back = await client.get("/api/agents/ag_gone")
    assert back.status_code == 200
    assert not back.json()["skills"]
