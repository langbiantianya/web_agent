"""V3.2 工具调用闭环的端到端验证：配置 → 模型要工具 → 执行 → 回灌 → 模型继续。

与 :mod:`tests.test_tools` 的分工：那边钉的是「工具本身算得对不对、名字对不对」，
这边钉的是**全链路**——HTTP 接口、SSE 事件、发给模型的请求体、DB 状态四者对得上。
两边的断言不能互相替代：工具算对了但回灌没接上，端到端照样是坏的。

LLM 后端是真实的 OpenAI 兼容 HTTP 服务（:mod:`tests.fake_llm`），应用侧起**真实
uvicorn**（不是 ASGITransport）：httpx 的 ``ASGITransport`` 会把整个响应体缓冲完
才返回，SSE 这种流式响应会被彻底堵死——工具闭环恰恰要中途观察事件。
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
from app.config.settings import settings
from app.main import create_app
from app.repo.run_repo import run_repo
from tests.fake_llm import FakeLLM


def _free_port() -> int:
    """向 OS 借一个空闲端口：固定端口会撞上残留进程或并行用例。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server():
    """在**测试自己的事件循环**里跑一个真实的 uvicorn 服务。

    ``uvicorn.Server.serve()`` 本身就是协程，``asyncio.create_task`` 就能和测试
    共用同一个事件循环：既拿到真实 socket 的流式语义，又不会引入跨事件循环的
    共享状态（hub、检查点、数据库连接全在同一个 loop 里）。

    刻意是**函数级**作用域：模块级的 async fixture 会与函数级的事件循环作用域
    打架（pytest-asyncio 报 loop scope 不一致）。每个用例起一次服务多一点
    开销，换来的是「每个用例都从干净的服务状态开始」。
    """
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
    """按用例创建独立的假 LLM，用完统一停掉。

    每个用例都要自己的一份假服务：``tool_calls_plan`` 是**按调用序**索引的，
    共用一份会让后一个用例的「第 1 次调用」对上不属于自己的计划。
    """
    started: list[FakeLLM] = []

    async def _make(**kwargs) -> FakeLLM:
        """起一个假 LLM 服务。

        Args:
            **kwargs: 透传给 :class:`FakeLLM` 的构造参数。

        Returns:
            已启动的假 LLM。
        """
        s = FakeLLM(**kwargs)
        s.start()
        started.append(s)
        return s

    yield _make

    for s in started:
        s.stop()


# ------------------------------------------------------------------ helpers


async def _make_agent(
    client: httpx.AsyncClient, code: str, llm: FakeLLM, tools: list[str] | None = None
):
    """建一个指向假 LLM 的 Agent，返回服务端返回的配置体。"""
    payload = {
        "code": code,
        "name": f"Agent {code}",
        "system_prompt": f"你是 {code}",
        "model": "fake-model",
        "base_url": llm.base_url,
        "api_key": "test-key",
    }
    if tools is not None:
        payload["tools"] = tools
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
            "name": "工具流水线",
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
                continue  # 心跳注释行，不占 seq
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("id:"):
                eid = int(line[3:].strip())
            elif line.startswith("data:"):
                out.append((event, eid, json.loads(line[5:].strip())))
                if event in ("done", "error"):
                    break
    return out


async def _wait_run_status(run_id: int, statuses: set[str], timeout: float = 15.0):
    """轮询等待 run 进入指定状态。

    每次轮询都开一个**新的短连接**并在读完立刻释放：跨 await 长时间持有读事务
    会阻塞 SQLite 的写者（编排器收尾时要 UPDATE run），把「等状态」变成死锁。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with SessionLocal() as s:
            run = await run_repo.get_by_id(s, run_id)
            if run is not None and run.status in statuses:
                return run
        await asyncio.sleep(0.02)
    return None


async def _run_once(
    client: httpx.AsyncClient, llm: FakeLLM, code: str, tools: list[str] | None
) -> tuple[int, int, list[tuple[str, int, dict]]]:
    """跑一个单节点流水线，返回 ``(会话 id, run id, SSE 事件列表)``。"""
    await _make_agent(client, code, llm, tools=tools)
    wf = await _make_workflow(client, [code])
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
    return conv["id"], run_id, events


# -------------------------------------------------------------------- 测试


async def test_agent_tools_config_is_validated_at_save_time(
    client: httpx.AsyncClient, llm_factory
):
    """未注册的工具名**保存期就拒**（400），配置根本进不了库。

    这是「工具名即契约」的全部意义：注册表里没有的名字如果能存进去，就会出现
    「模型看得见、服务端执行不了」的工具——错误被推迟到运行时才炸，那时 token
    和钱已经花出去了。
    """
    llm = await llm_factory(texts=["ok"])

    bad = await client.post(
        "/api/agents",
        json={
            "code": "tool_bad",
            "name": "坏配置",
            "system_prompt": "x",
            "model": "m",
            "base_url": llm.base_url,
            "tools": ["不存在的工具"],
        },
    )
    assert bad.status_code == 400, bad.text
    body = bad.json()
    assert body["code"] == "bad_request"
    assert "未知工具" in body["message"]
    # 报错要告诉用户有哪些可选
    assert "calc" in body["message"]

    # 合法配置存得进去，也读得回来
    good = await client.post(
        "/api/agents",
        json={
            "code": "tool_good",
            "name": "好配置",
            "system_prompt": "x",
            "model": "m",
            "base_url": llm.base_url,
            "tools": ["calc", "now"],
        },
    )
    assert good.status_code == 201, good.text
    assert good.json()["tools"] == ["calc", "now"]

    # 更新时也要校验
    upd = await client.put("/api/agents/tool_good", json={"tools": ["瞎写的"]})
    assert upd.status_code == 400, upd.text
    assert "未知工具" in upd.json()["message"]


async def test_tools_endpoint_lists_registry(client: httpx.AsyncClient):
    """工具目录接口：后台据此渲染勾选框，而不是让用户凭记忆敲名字。"""
    r = await client.get("/api/tools")
    assert r.status_code == 200, r.text
    body = r.json()
    names = [t["name"] for t in body]
    assert "now" in names
    assert "calc" in names
    for item in body:
        assert item["description"]
        assert item["parameters"]["type"] == "object"


async def test_tool_call_is_executed_and_fed_back_to_model(
    client: httpx.AsyncClient, llm_factory
):
    """闭环全链路：模型要工具 → 执行 → 结果回灌 → 模型继续答。

    断言三件事，缺一不可：
    1. **配置的工具名真的展开成了 schema 发给模型**（否则模型根本不知道有工具）；
    2. **工具结果真的进了下一轮请求的 messages**（这是闭环的关键，回灌断了
       模型就永远看不到结果，只能瞎编）；
    3. **SSE 上有 tool_call / tool_result 事件**（前端才有东西可显示）。
    """
    llm = await llm_factory(
        # 假模型按**调用序**取 texts：第 0 次调用要走 tool_calls_plan（不吐文本），
        # 所以 texts[0] 用不上，答案在 texts[1]
        texts=["占位", "这是最终答案"],
        tool_calls_plan=[
            [
                {
                    "id": "call_1",
                    "name": "calc",
                    "arguments": '{"expression": "(12 + 8) * 3"}',
                }
            ]
        ],
    )
    base = len(llm.calls)

    conv_id, run_id, events = await _run_once(client, llm, "tool_calc", tools=["calc"])
    kinds = [e for e, _, _ in events]
    assert "tool_call" in kinds, kinds
    assert "tool_result" in kinds, kinds
    assert kinds[-1] == "done"

    # 1) 配置的工具名被展开成 OpenAI function schema 发给了模型
    assert len(llm.calls) == base + 2, "应该正好调了模型两轮"
    first_request = llm.calls[base]
    assert first_request["tools"][0]["function"]["name"] == "calc"
    assert first_request["tools"][0]["function"]["parameters"]["required"] == [
        "expression"
    ]

    # 2) 第二轮请求里带着 role=tool 的工具结果
    second_request = llm.calls[base + 1]
    tool_msgs = [m for m in second_request["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, second_request["messages"]
    assert tool_msgs[0]["content"] == "60", tool_msgs[0]
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    # 模型那轮要的 assistant 消息也要带回去，且带上了配对的 tool_calls
    assistants = [
        m for m in second_request["messages"] if m.get("role") == "assistant"
    ]
    assert assistants and assistants[0]["tool_calls"][0]["id"] == "call_1"

    # 3) tool_result 事件说清了「调了哪个工具、成没成」
    results = [d for e, _, d in events if e == "tool_result"]
    assert results[0]["tool"] == "calc"
    assert results[0]["ok"] is True
    assert "60" in results[0]["summary"]

    # run 正常结束，节点产出就是各轮拼接的文本
    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "run 未正常结束"
    async with SessionLocal() as db:
        steps = await run_repo.list_steps(db, run_id)
        assert len(steps) == 1
        assert steps[0].status == "done"
        assert steps[0].output_json["toolRounds"] == 1
        assert steps[0].output_json["toolCalls"][0]["function"]["name"] == "calc"

    # 流文本 == 库文本（§14.3 依赖的性质，工具闭环不能把它破坏掉）
    streamed = "".join(d["content"] for e, _, d in events if e == "token")
    msgs = (await client.get(f"/api/conversations/{conv_id}/messages")).json()
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == streamed == "这是最终答案"


async def test_tool_error_is_fed_back_not_fatal(
    client: httpx.AsyncClient, llm_factory
):
    """工具算错**不失败节点**：错误文本回灌给模型，让它有机会自己纠正。

    抛异常会让整个节点挂掉，而模型其实能看见「除数为零」并改用别的方式回答。
    真正兜不住的是「模型一直调同一个必失败的工具」，那由轮次上限收口。
    """
    llm = await llm_factory(
        texts=["那就换个说法", "备用"],
        tool_calls_plan=[
            [{"id": "call_e", "name": "calc", "arguments": '{"expression": "1/0"}'}]
        ],
    )
    base = len(llm.calls)

    _, run_id, events = await _run_once(client, llm, "tool_err", tools=["calc"])

    results = [d for e, _, d in events if e == "tool_result"]
    assert results[0]["ok"] is False
    assert "除数为零" in results[0]["summary"]

    # 错误确实回灌给了模型
    second_request = llm.calls[base + 1]
    tool_msgs = [m for m in second_request["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "除数为零" in tool_msgs[0]["content"]

    # 节点没挂：run 正常结束
    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "工具出错不该让 run 失败"


async def test_tool_loop_limit_fails_without_retry(
    client: httpx.AsyncClient, llm_factory, monkeypatch
):
    """模型一直要工具 → 轮次上限收口 → run 失败，且**不重试**。

    重试同一个节点，模型大概率还是同样地陷住，白烧一轮 token。所以这里的
    断言是「模型恰好被调了 tool_max_rounds 次」——多一次就说明重试了。
    """
    monkeypatch.setattr(settings, "tool_max_rounds", 2)
    llm = await llm_factory(
        texts=["备用"],
        tool_calls_plan=[
            [{"id": f"call_loop{i}", "name": "calc", "arguments": '{"expression": "1+1"}'}]
            for i in range(5)
        ],
    )
    base = len(llm.calls)

    _, run_id, events = await _run_once(client, llm, "tool_loop", tools=["calc"])

    # 恰好两轮：既没超预算，也没有重试
    assert len(llm.calls) - base == 2, f"模型被调了 {len(llm.calls) - base} 次"

    kinds = [e for e, _, _ in events]
    assert "error" in kinds, kinds
    errors = [d for e, _, d in events if e == "error"]
    assert errors[0]["type"] == "ToolLoopExceeded"
    assert "陷住" in errors[0]["message"]

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "error"
    assert "工具调用超过 2 轮" in (run.error_msg or ""), run.error_msg

    # 节点记 error，且错误原因留下来了
    async with SessionLocal() as db:
        steps = await run_repo.list_steps(db, run_id)
        assert steps[0].status == "error"
        assert "陷住" in (steps[0].error_msg or "")


async def test_now_tool_returns_current_time(
    client: httpx.AsyncClient, llm_factory
):
    """now 工具：模型问时间，工具真的给出当前时间。"""
    llm = await llm_factory(
        texts=["现在是……", "备用"],
        tool_calls_plan=[[{"id": "call_n", "name": "now", "arguments": "{}"}]],
    )
    base = len(llm.calls)

    _, run_id, events = await _run_once(client, llm, "tool_now", tools=["now"])

    second_request = llm.calls[base + 1]
    tool_msgs = [m for m in second_request["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "UTC" in tool_msgs[0]["content"]

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done"
