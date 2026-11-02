"""技能正文注入提示词的端到端验证（设计文档 §22.6）。

要钉住的命题只有一个：**发给模型的 messages 里，技能正文确实在**。

单元测试能证明「拼装函数产出正确」，但证明不了「编排器真的把它拼进了发给模型的
请求里」——这两者之间隔着一层 messages 构造，而那层一旦漏掉，技能就是个摆设，
还不会报错。

所以这里起**真实 uvicorn + 假模型**，直接断言假模型收到的请求体。

LLM 后端是真实的 OpenAI 兼容 HTTP 服务（:mod:`tests.fake_llm`），应用侧也起
真实 uvicorn：``httpx`` 的 ``ASGITransport`` 会把整个响应体缓冲完才返回，SSE
这种流式响应会被彻底堵死。
"""

from __future__ import annotations

import asyncio
import json
import socket
import time

import httpx
import pytest
import uvicorn

from app.config.db import SessionLocal
from app.main import create_app
from app.repo.skill_repo import skill_repo
from tests.fake_llm import FakeLLM


def _free_port() -> int:
    """向 OS 借一个空闲端口：固定端口会撞上残留进程或并行用例。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server():
    """在**测试自己的事件循环**里跑一个真实的 uvicorn 服务。"""
    port = _free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="error"
    )
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())

    deadline = time.time() + 15
    while time.time() < deadline and not getattr(srv, "started", False):
        await asyncio.sleep(0.02)
    if not getattr(srv, "started", False):
        srv.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        raise RuntimeError("应用启动超时")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        await asyncio.wait_for(task, timeout=15)


@pytest.fixture
async def client(server: str):
    """真实 HTTP 客户端（真 socket，不是 ASGITransport）。"""
    async with httpx.AsyncClient(base_url=server, timeout=30) as c:
        yield c


@pytest.fixture
async def llm_factory():
    """按用例创建独立的假 LLM，用完统一停掉。"""
    started: list[FakeLLM] = []

    async def _make(**kwargs) -> FakeLLM:
        """起一个假 LLM 并登记到清理列表。"""
        s = FakeLLM(**kwargs)
        s.start()
        started.append(s)
        return s

    yield _make

    for s in started:
        s.stop()


# ------------------------------------------------------------------ helpers


async def _make_skill(code: str, body: str) -> str:
    """建一个技能，返回它的 code。"""
    async with SessionLocal() as db:        await skill_repo.create(
            db,
            code=code,
            name=code,
            description="演示技能",
            body=body,
            content_hash="h",
            origin="manual",
        )
    return code


async def _make_agent(
    client: httpx.AsyncClient, code: str, llm: FakeLLM, skills: list[str] | None = None
):
    """建一个指向假 LLM 的 Agent。"""
    payload = {
        "code": code,
        "name": f"Agent {code}",
        "system_prompt": f"你是 {code}",
        "model": "fake-model",
        "base_url": llm.base_url,
        "api_key": "test-key",
    }
    if skills is not None:
        payload["skills"] = skills
    r = await client.post("/api/agents", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _make_workflow(client: httpx.AsyncClient, codes: list[str]):
    """按给定顺序串一条线性流水线。"""
    nodes = [{"id": c, "agentCode": c, "outputKey": f"{c}_out"} for c in codes]
    edges = [{"from": a, "to": b} for a, b in zip(codes, codes[1:])]
    r = await client.post(
        "/api/workflows",
        json={
            "code": f"wf_{'_'.join(codes)}",
            "name": "技能流水线",
            "definition": {
                "entry": codes[0],
                "nodes": nodes,
                "edges": edges,
                "inputTemplate": "问题：{{user_input}}",
            },
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _new_conversation(client: httpx.AsyncClient, wf_code: str):
    """开一个新会话。"""
    r = await client.post(
        "/api/conversations", json={"workflow_code": wf_code, "user_id": "u1"}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _ticket(client: httpx.AsyncClient) -> str:
    """取一张一次性 SSE 凭证。"""
    r = await client.post("/api/auth/sse-ticket", json={"user_id": "u1"})
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


async def _read_sse(client: httpx.AsyncClient, url: str) -> list[tuple[str, int, dict]]:
    """读取 SSE 流，返回 [(event, id, data), ...]。"""
    out: list[tuple[str, int, dict]] = []
    async with client.stream("GET", url) as resp:
        assert resp.status_code == 200, await resp.aread()
        event, eid = "message", 0
        async for line in resp.aiter_lines():
            line = line.rstrip("\r")
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("id:"):
                eid = int(line[3:].strip())
            elif line.startswith("data:"):
                out.append((event, eid, json.loads(line[5:].strip())))
                if event in ("done", "error"):
                    break
    return out


# -------------------------------------------------------------------- 测试


async def test_skill_body_is_injected_as_own_system_message(
    client: httpx.AsyncClient, llm_factory
):
    """技能正文作为**独立的一条** system 消息，夹在系统提示词与历史之间。

    顺序是 system_prompt → 技能正文 → 会话历史 → user 输入。技能正文独立成条
    是为了后台「生效提示词预览」与实际发送逐字一致——拼进 system_prompt 字段的
    做法会让运营看到的和模型收到的不一样。
    """
    llm = await llm_factory(texts=["收到"])
    await _make_skill("inj_skill", "技能正文：先读 metrics 再下结论。")

    await _make_agent(client, "inj_agent", llm, skills=["inj_skill"])
    wf = await _make_workflow(client, ["inj_agent"])
    conv = await _new_conversation(client, wf["code"])
    sent = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "算一下"}
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run_id"]
    ticket = await _ticket(client)
    events = await _read_sse(
        client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0"
    )
    assert events[-1][0] == "done", events

    messages = llm.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "你是 inj_agent"
    assert messages[1]["role"] == "system"
    assert "技能正文：先读 metrics 再下结论。" in messages[1]["content"]
    assert messages[-1]["role"] == "user"
    assert "算一下" in messages[-1]["content"]


async def test_no_skill_means_no_extra_system_message(
    client: httpx.AsyncClient, llm_factory
):
    """没绑技能时，messages 的形状与以前一模一样——不多一条 system。"""
    llm = await llm_factory(texts=["收到"])
    await _make_agent(client, "noskill_agent", llm)
    wf = await _make_workflow(client, ["noskill_agent"])
    conv = await _new_conversation(client, wf["code"])
    sent = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "你好"}
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run_id"]
    ticket = await _ticket(client)
    await _read_sse(client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0")

    messages = llm.calls[0]["messages"]
    assert messages[0]["content"] == "你是 noskill_agent"
    assert len([m for m in messages if m["role"] == "system"]) == 1


async def test_disabled_skill_is_not_injected(
    client: httpx.AsyncClient, llm_factory
):
    """停用的技能不进提示词——绑着不等于生效。"""
    llm = await llm_factory(texts=["收到"])
    await _make_skill("off_inj", "停用技能的正文不该出现。")
    await _make_agent(client, "off_agent", llm, skills=["off_inj"])
    # 技能建好后停用
    async with SessionLocal() as db:
        from app.repo.skill_repo import skill_repo as repo

        await repo.update(db, "off_inj", {"enabled": 0})

    wf = await _make_workflow(client, ["off_agent"])
    conv = await _new_conversation(client, wf["code"])
    sent = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "你好"}
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run_id"]
    ticket = await _ticket(client)
    await _read_sse(client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0")

    messages = llm.calls[0]["messages"]
    assert "停用技能的正文不该出现。" not in json.dumps(messages, ensure_ascii=False)


async def test_skipped_skill_emits_event(
    client: httpx.AsyncClient, llm_factory, monkeypatch
):
    """技能被预算挡掉时，事件流里有一条 `skill_skipped` 说明是哪个、为什么。

    技能被**静默**丢掉是最坏的一种情况：配置里明明绑了，模型却没拿到任何
    指令，而界面上看不出任何异常。
    """
    from app.config.settings import settings

    monkeypatch.setattr(settings, "skill_max_count", 1)
    llm = await llm_factory(texts=["收到"])
    await _make_skill("skip_first", "第一个技能。")
    await _make_skill("skip_second", "第二个技能。")

    await _make_agent(
        client, "skip_agent", llm, skills=["skip_first", "skip_second"]
    )
    wf = await _make_workflow(client, ["skip_agent"])
    conv = await _new_conversation(client, wf["code"])
    sent = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "你好"}
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run_id"]
    ticket = await _ticket(client)
    events = await _read_sse(
        client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0"
    )

    skipped = [d for e, _, d in events if e == "skill_skipped"]
    assert skipped, [e for e, _, _ in events]
    assert skipped[0]["skill"] == "skip_second"
    assert "上限" in skipped[0]["reason"]

    # 被挡掉的技能正文确实没进提示词
    messages = llm.calls[0]["messages"]
    assert "第一个技能。" in json.dumps(messages, ensure_ascii=False)
    assert "第二个技能。" not in json.dumps(messages, ensure_ascii=False)
