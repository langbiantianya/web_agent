"""上下文装配与 token 估算（设计文档 §4.4）。"""

from __future__ import annotations

import itertools

import pytest

from app.config.db import SessionLocal
from app.models.conversation import Conversation
from app.models.enums import MessageRole, MessageStatus
from app.repo.message_repo import estimate_tokens, message_repo

_convs = itertools.count(950_000)


@pytest.fixture
async def conversation_id() -> int:
    """造一个独立会话，避免依赖其它用例的数据。"""
    cid = next(_convs)
    async with SessionLocal() as db:
        db.add(Conversation(id=cid, session_id=f"s-{cid}", workflow_id=0, user_id="t"))
        await db.commit()
    return cid


# ------------------------------------------------------------- token 估算


def test_estimate_tokens_handles_cjk_and_ascii():
    """中英混排的粗估：CJK 约 1 字 1 token、ASCII 约 4 字符 1 token。

    这个估算决定截断多少历史，宁可粗略也要稳定——不能返回 None 或负数。
    """
    assert estimate_tokens("你好世界") == 4  # 中文 1 字 ≈ 1 token
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    ascii_est = estimate_tokens("a" * 400)
    assert 95 <= ascii_est <= 105  # 约 4 字符 / token


# ------------------------------------------------------------- 上下文装配


async def test_load_context_preserves_order(conversation_id: int):
    """喂给模型的历史必须是严格的时间顺序。

    顺序错了（变成倒序或按 id 乱排）会让模型读到「先回答后提问」的对话。
    """
    async with SessionLocal() as db:
        for i in range(4):
            await message_repo.add(
                db,
                conversation_id=conversation_id,
                run_id=None,
                agent_code=None,
                role=MessageRole.USER.value if i % 2 == 0 else MessageRole.ASSISTANT.value,
                content=f"消息{i}",
                status=MessageStatus.DONE.value,
            )
        await db.commit()

        ctx = await message_repo.load_context(db, conversation_id)
        assert [m["content"] for m in ctx] == ["消息0", "消息1", "消息2", "消息3"]


async def test_load_context_respects_token_budget(conversation_id: int):
    """超出预算时只回溯最近的若干条，且从**最新一条**开始保留。

    防的是「从最老的一条开始截」——那样保留下来的是过时上下文。
    """
    async with SessionLocal() as db:
        for i in range(20):
            await message_repo.add(
                db,
                conversation_id=conversation_id,
                run_id=None,
                agent_code=None,
                role=MessageRole.USER.value,
                content="内容" * 50,  # 每条约 100 token
                status=MessageStatus.DONE.value,
            )
        await db.commit()

        ctx = await message_repo.load_context(db, conversation_id, max_tokens=350)
        assert 0 < len(ctx) <= 4, "应只回溯到预算内的最近几条"
        assert ctx[-1]["content"].startswith("内容")


async def test_load_context_keeps_newest_when_over_budget(conversation_id: int):
    """预算不够时，保留的必须是**最近**的几条，最旧的先被丢掉。

    上一条用例 20 条消息的内容完全一样，方向反了也测不出来——断言
    「content 以『内容』开头」对新旧两种实现都成立。这里给每条消息打上
    可区分的编号，才能真正钉住「留新的、丢旧的」。

    曾经这里是反的：rows 是 id 倒序（新→旧），代码却从最旧那条开始累加、
    超预算即 break，等于「预算不够就砍掉最近的、把最老的留着」。实测 10 条
    消息、预算 350 token 时返回的是第 0～2 号，最近的第 7～9 号全丢。
    """
    async with SessionLocal() as db:
        for i in range(10):
            await message_repo.add(
                db,
                conversation_id=conversation_id,
                run_id=None,
                agent_code=None,
                role=MessageRole.USER.value,
                content=f"【第{i}号】" + "填充" * 50,  # 每条约 103 token
                status=MessageStatus.DONE.value,
            )
        await db.commit()

        ctx = await message_repo.load_context(db, conversation_id, max_tokens=350)
        contents = [m["content"] for m in ctx]
        assert len(contents) <= 3, f"预算只够 3 条，实际 {len(contents)}"
        assert contents[-1].startswith("【第9号】"), contents
        assert contents[0].startswith("【第7号】"), contents
        assert not any("【第0号】" in c for c in contents), contents


async def test_load_context_always_keeps_the_newest(conversation_id: int):
    """最新的一条**无条件保留**，哪怕它自己就超预算。

    它往往正是本轮的用户输入——被丢掉之后，编排器连「pop 掉本轮输入」这一步
    都做不成，本轮内容会被重复喂给模型。
    """
    async with SessionLocal() as db:
        await message_repo.add(
            db,
            conversation_id=conversation_id,
            run_id=None,
            agent_code=None,
            role=MessageRole.USER.value,
            content="【最新】" + "超长" * 2000,  # 远超预算
            status=MessageStatus.DONE.value,
        )
        await db.commit()

        ctx = await message_repo.load_context(db, conversation_id, max_tokens=100)
        assert len(ctx) == 1, "单条超预算也不该把历史清空"
        assert ctx[0]["content"].startswith("【最新】")


async def test_load_context_skips_streaming_messages(conversation_id: int):
    """上一个 run 的半成品（streaming）不能被当成事实喂回模型。"""
    async with SessionLocal() as db:
        await message_repo.add(
            db, conversation_id=conversation_id, run_id=None, agent_code=None,
            role=MessageRole.USER.value, content="已完成的", status=MessageStatus.DONE.value,
        )
        await message_repo.add(
            db, conversation_id=conversation_id, run_id=None, agent_code=None,
            role=MessageRole.ASSISTANT.value, content="半成品", status=MessageStatus.STREAMING.value,
        )
        await db.commit()

        ctx = await message_repo.load_context(db, conversation_id)
        assert [m["content"] for m in ctx] == ["已完成的"]


async def test_load_context_can_include_streaming(conversation_id: int):
    """关掉 ``exclude_pending`` 时半成品要被带上——当前 run 续跑时需要它。

    与上一个用例配对：防的是过滤条件写死成「永远排除 streaming」。
    """
    async with SessionLocal() as db:
        await message_repo.add(
            db, conversation_id=conversation_id, run_id=None, agent_code=None,
            role=MessageRole.ASSISTANT.value, content="半成品", status=MessageStatus.STREAMING.value,
        )
        await db.commit()
        ctx = await message_repo.load_context(
            db, conversation_id, exclude_pending=False
        )
        assert [m["content"] for m in ctx] == ["半成品"]


async def test_token_count_is_computed_on_insert(conversation_id: int):
    """token_count 在插入时就落库，而不是每次统计时重算。

    否则列表页每渲染一条消息都要读全文算 token，长会话直接卡住。
    """
    async with SessionLocal() as db:
        msg = await message_repo.add(
            db, conversation_id=conversation_id, run_id=None, agent_code=None,
            role=MessageRole.USER.value, content="一二三四五六", status=MessageStatus.DONE.value,
        )
        await db.commit()
        assert msg.token_count == 6


async def test_list_by_conversation_is_paged(conversation_id: int):
    """翻页是「按固定步长切片」而不是「从某条开始无限返回」。

    offset 算错会让第二页漏消息或重复消息，历史记录里出现「串页」。
    """
    async with SessionLocal() as db:
        for i in range(10):
            await message_repo.add(
                db, conversation_id=conversation_id, run_id=None, agent_code=None,
                role=MessageRole.USER.value, content=f"m{i}", status=MessageStatus.DONE.value,
            )
        await db.commit()

        first = await message_repo.list_by_conversation(db, conversation_id, limit=3)
        second = await message_repo.list_by_conversation(
            db, conversation_id, limit=3, offset=3
        )
        assert [m.content for m in first] == ["m0", "m1", "m2"]
        assert [m.content for m in second] == ["m3", "m4", "m5"]
