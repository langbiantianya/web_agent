"""Skill 仓储层（设计文档 §22.5）。"""

from __future__ import annotations

import itertools

from app.repo.agent_repo import agent_repo
from app.repo.skill_repo import skill_repo

# 测试库是 session 级建表、**用例之间不清理**，所以每个用例要用自己的 code，
# 否则第二个用例建同名技能会撞唯一约束。
_counter = itertools.count()


def _code(prefix: str = "sk") -> str:
    """生成一个本进程内唯一的技能 code。"""
    return f"{prefix}_{next(_counter)}"


async def _make(db, code: str | None = None):
    """建一个技能，返回它。"""
    return await skill_repo.create(
        db,
        code=code or _code(),
        name="演示",
        description="演示",
        body="正文",
        content_hash="h",
        origin="manual",
    )


async def test_create_and_get(db):
    """建了能按 code 取回。"""
    code = _code()
    sk = await _make(db, code)
    got = await skill_repo.get_by_code(db, code)
    assert got is not None
    assert got.id == sk.id
    assert got.body == "正文"


async def test_get_by_code_missing_returns_none(db):
    """不存在的 code 返回 None，不抛。"""
    assert await skill_repo.get_by_code(db, "nope") is None


async def test_update_bumps_version(db):
    """更新时 version 原子 +1（与 workflow 同口径）。"""
    code = _code()
    await _make(db, code)
    updated = await skill_repo.update(db, code, {"body": "新正文"})
    assert updated is not None
    assert updated.version == 2
    assert updated.body == "新正文"


async def test_update_missing_returns_none(db):
    """改不存在的技能返回 None，由 API 层转 404。"""
    assert await skill_repo.update(db, "nope", {"body": "x"}) is None


async def test_list_only_enabled(db):
    """只列启用的技能。"""
    on = _code("on")
    off = _code("off")
    await _make(db, on)
    await skill_repo.create(
        db, code=off, name="off", description="d", body="b", content_hash="h", enabled=0
    )
    codes = [s.code for s in await skill_repo.list(db, only_enabled=True)]
    assert on in codes
    assert off not in codes


async def test_list_by_codes_keeps_input_order(db):
    """按 code 集合取技能时按传入顺序返回——顺序 = 后台排的优先级。"""
    a, b, c = _code("a"), _code("b"), _code("c")
    await _make(db, a)
    await _make(db, b)
    await _make(db, c)
    got = await skill_repo.list_by_codes(db, [c, a])
    assert [s.code for s in got] == [c, a]


async def test_list_by_codes_skips_unknown(db):
    """找不到的 code 直接略过——读路径要宽容，不能让整个节点崩掉。"""
    a = _code("a")
    await _make(db, a)
    got = await skill_repo.list_by_codes(db, [a, "gone"])
    assert [s.code for s in got] == [a]


async def test_put_resource_is_upsert(db):
    """同一个 path 写两次是覆盖，不是新增。"""
    sk = await _make(db)
    await skill_repo.put_resource(db, sk.id, "a.md", b"one", "text/markdown", 1)
    await skill_repo.put_resource(db, sk.id, "a.md", b"two", "text/markdown", 1)
    rows = await skill_repo.list_resources(db, sk.id)
    assert len(rows) == 1
    assert rows[0].content == b"two"
    assert rows[0].size_bytes == 3


async def test_delete_cascades_resources(db):
    """删技能要连资源与修订历史一起删——不留孤儿行。"""
    code = _code()
    sk = await _make(db, code)
    await skill_repo.put_resource(db, sk.id, "a.md", b"one", "text/markdown", 1)
    await skill_repo.add_revision(db, sk.id, 1, "b", [], "admin", "init")
    assert await skill_repo.delete(db, code) is True
    assert await skill_repo.get_by_code(db, code) is None
    assert await skill_repo.list_resources(db, sk.id) == []
    assert await skill_repo.list_revisions(db, sk.id) == []


async def test_delete_missing_returns_false(db):
    """删不存在的技能返回 False。"""
    assert await skill_repo.delete(db, "nope") is False


async def test_total_resource_bytes(db):
    """资源总量用于上限校验。"""
    sk = await _make(db)
    await skill_repo.put_resource(db, sk.id, "a.md", b"12345", "text/markdown", 1)
    await skill_repo.put_resource(db, sk.id, "b.md", b"678", "text/markdown", 1)
    assert await skill_repo.total_resource_bytes(db, sk.id) == 8
    # 替换某个资源时，它自己的旧大小不该算进总量
    assert await skill_repo.total_resource_bytes(db, sk.id, exclude_path="a.md") == 3


async def test_revision_roundtrip(db):
    """修订快照要能按版本号取回来——回滚就是把它读出来写回去。"""
    sk = await _make(db)
    await skill_repo.add_revision(
        db,
        sk.id,
        1,
        "第一版",
        [{"path": "a.md", "sha256": "x", "is_text": True}],
        "admin",
        "创建",
    )
    got = await skill_repo.get_revision(db, sk.id, 1)
    assert got is not None
    assert got.body == "第一版"
    assert got.resources[0]["path"] == "a.md"
    assert got.author == "admin"


async def test_list_revisions_newest_first(db):
    """修订历史按版本倒序。"""
    sk = await _make(db)
    await skill_repo.add_revision(db, sk.id, 1, "v1", [], "admin", "")
    await skill_repo.add_revision(db, sk.id, 2, "v2", [], "admin", "")
    rows = await skill_repo.list_revisions(db, sk.id)
    assert [r.version for r in rows] == [2, 1]


async def test_count_agents_using(db):
    """统计有几个 Agent 绑着这个技能。"""
    code = _code()
    agent_code = _code("ag")
    await agent_repo.create(
        db,
        code=agent_code,
        name="带技能的 agent",
        system_prompt="x",
        model="m",
        base_url="http://x",
        api_key_cipher="c",
        skills=[code],
    )
    assert await skill_repo.count_agents_using(db, code) == 1
