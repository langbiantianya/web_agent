"""V3.1 人工审批的端到端验证：跑 → 挂起 → 批准 → 继续 → 完成 / 驳回 → 终止。

与 :mod:`tests.test_approval` 的分工：那边钉的是「图与挂起语义」（直接调
``run_graph``），这边钉的是**全链路**——HTTP 接口、SSE 事件、DB 状态三者对得上。
两边的断言不能互相替代：图跑对了但接口没接线，端到端照样是坏的。

LLM 后端是真实的 OpenAI 兼容 HTTP 服务（:mod:`tests.fake_llm`），应用侧走
ASGITransport + **真实 lifespan**——checkpointer 正是在 lifespan 里初始化的，
跳过它就等于没测审批功能。
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
from app.models.enums import RunStatus
from app.repo.run_approval_repo import run_approval_repo
from app.repo.run_repo import run_repo
from tests.fake_llm import FakeLLM


def _free_port() -> int:
    """向 OS 借一个空闲端口：固定端口会撞上残留进程或并行用例。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def llm() -> FakeLLM:
    """整个模块共用一个假 LLM 服务。

    scope=module：每个用例都重启一次 HTTP 服务太慢，而假服务本身无状态
    （按调用序返回 texts），只有 calls 记录会跨用例累积——用例里用调用序的
    差值来断言「有没有重跑」。
    """
    server = FakeLLM(
        texts=[f"第{i}段输出" for i in range(20)],
        chunk_delay=0.0,
    )
    server.start()
    yield server
    server.stop()


@pytest.fixture
async def server():
    """在**测试自己的事件循环**里跑一个真实的 uvicorn 服务。

    ## 为什么不用 ASGITransport

    httpx 的 ``ASGITransport`` 会把**整个响应体缓冲完**才返回（源码里
    ``await self.app(...)`` 跑完之后才组装 ``Response``），SSE 这种「中途要
    交互、很久才结束」的流会被彻底堵死。审批用例恰恰要中途提交决策，缓冲式
    传输根本表达不了这件事——实测的表现是客户端一个事件都收不到，直接死锁。

    ## 为什么不用子进程

    ``uvicorn.Server.serve()`` 本身就是协程，``asyncio.create_task`` 就能和测试
    共用同一个事件循环：既拿到真实 socket 的流式语义，又不会引入跨事件循环的
    共享状态（hub、检查点、数据库连接全在同一个 loop 里）。
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
    """真实 HTTP 客户端（真 socket，不是 ASGITransport）。

    read 超时放宽到 60 秒：SSE 连接会一直开着等人审批，不是「卡住」。
    """
    async with httpx.AsyncClient(
        base_url=server, timeout=httpx.Timeout(30, read=60)
    ) as c:
        yield c


# ------------------------------------------------------------------ helpers


async def _make_agent(client: httpx.AsyncClient, code: str, llm: FakeLLM) -> None:
    """建一个指向假 LLM 的 Agent。"""
    r = await client.post(
        "/api/agents",
        json={
            "code": code,
            "name": f"Agent {code}",
            "system_prompt": f"你是 {code}",
            "model": "fake-model",
            "base_url": llm.base_url,
            "api_key": "test-key",
        },
    )
    assert r.status_code == 201, r.text


def _definition(plan_code: str, write_code: str) -> dict:
    """「规划 → 人工审批 → 撰写」三段流程。"""
    return {
        "entry": "plan",
        "nodes": [
            {"id": "plan", "agentCode": plan_code, "outputKey": "plan_out"},
            {
                "id": "gate",
                "type": "approval",
                "prompt": "这份计划可以执行吗？",
                "outputKey": "gate_out",
            },
            {"id": "write", "agentCode": write_code, "outputKey": "answer"},
        ],
        "edges": [
            {"from": "plan", "to": "gate"},
            {"from": "gate", "to": "write"},
        ],
        "inputTemplate": "问：{{user_input}}",
    }


async def _make_workflow(client: httpx.AsyncClient, code: str, definition: dict) -> None:
    """保存工作流。"""
    r = await client.post(
        "/api/workflows",
        json={"code": code, "name": "审批流水线", "definition": definition},
    )
    assert r.status_code == 201, r.text


async def _start_run(client: httpx.AsyncClient, wf_code: str) -> int:
    """开会话 + 发消息，返回 run_id。"""
    conv = (
        await client.post(
            "/api/conversations", json={"workflow_code": wf_code, "user_id": "u1"}
        )
    ).json()
    r = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "帮我写一篇稿子"}
    )
    # 202：消息已受理，run 异步生成
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


async def _ticket(client: httpx.AsyncClient) -> str:
    """取一张一次性 SSE 凭证。"""
    r = await client.post("/api/auth/sse-ticket", json={"user_id": "u1"})
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


async def _read_sse(client: httpx.AsyncClient, url: str, on_approval=None) -> list[dict]:
    """读 SSE 流，返回事件列表；遇到 ``approval_required`` 就调 ``on_approval``。

    回调在流里触发而不是流外：审批之后 run 要**接着往下跑**，而后续事件仍然
    走这同一条连接——这正是「批准后同一条流接着吐字」要验的点。把流掐断再
    重连就没验到它了。
    """
    out: list[dict] = []
    async with client.stream("GET", url) as resp:
        assert resp.status_code == 200, await resp.aread()
        etype = "message"
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                etype = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                out.append({"event": etype, **data})
                if etype == "approval_required" and on_approval is not None:
                    await on_approval(data)
                if etype == "done":
                    break
    return out


async def _wait_status(
    client: httpx.AsyncClient, run_id: int, want: str, timeout: float = 5.0
) -> str:
    """轮询 run 状态直到变成 ``want``，返回它。

    为什么是轮询而不是读一次：``approval_required`` 事件是在 run 状态写成
    ``waiting`` **之前**发出去的（事件要赶在前面让前端亮卡片），客户端收到
    事件的那一刻状态可能还停在 running。
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = await client.get(f"/api/runs/{run_id}")
        last = r.json()["status"]
        if last == want:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} 的状态没等到 {want}，最后是 {last}")


async def _decide(
    client: httpx.AsyncClient, run_id: int, approved: bool, comment: str
) -> httpx.Response:
    """提交一次审批决策。"""
    return await client.post(
        f"/api/runs/{run_id}/approval",
        json={"approved": approved, "comment": comment, "decidedBy": "tester"},
    )


# ------------------------------------------------------------------- 用例


async def test_approve_resumes_run_to_completion(client: httpx.AsyncClient, llm: FakeLLM):
    """批准 → run 从暂停点继续 → 跑完 → done。"""
    tag = f"ap{int(time.time() * 1000) % 1000000}"
    await _make_agent(client, f"{tag}_plan", llm)
    await _make_agent(client, f"{tag}_write", llm)
    await _make_workflow(
        client,
        f"{tag}_wf",
        _definition(f"{tag}_plan", f"{tag}_write"),
    )

    run_id = await _start_run(client, f"{tag}_wf")
    calls_before = len(llm.calls)
    ticket = await _ticket(client)

    submitted: dict = {}

    async def on_approval(payload: dict) -> None:
        """收到审批请求：核对状态与待办，然后批准。"""
        submitted["payload"] = payload
        await _wait_status(client, run_id, RunStatus.WAITING.value)

        pending = await client.get(f"/api/runs/{run_id}/approval")
        assert pending.status_code == 200, pending.text
        body = pending.json()
        assert body is not None, "挂起后应有待审批项"
        assert body["status"] == "pending"
        assert body["prompt"] == "这份计划可以执行吗？"
        assert body["node_id"] == "gate"

        r = await _decide(client, run_id, True, "可以执行")
        assert r.status_code == 200, r.text
        submitted["decision"] = r.json()

    events = await _read_sse(
        client,
        f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0",
        on_approval=on_approval,
    )

    kinds = [e["event"] for e in events]
    assert "approval_required" in kinds, f"没收到审批事件：{kinds}"
    assert kinds[-1] == "done", f"流没以 done 收尾：{kinds}"

    done = events[-1]
    assert done.get("status") == RunStatus.DONE.value, done
    assert submitted["decision"]["approved"] is True
    assert submitted["decision"]["status"] == "approved"
    assert submitted["decision"]["comment"] == "可以执行"

    # 恢复不重走前面的节点：批准后只应新增「撰写」那一次模型调用
    assert len(llm.calls) - calls_before == 2, (
        f"批准后模型应只被调用 2 次（规划 + 撰写），实际 "
        f"{len(llm.calls) - calls_before} 次"
    )

    await _wait_status(client, run_id, RunStatus.DONE.value)

    # 审批意见要落进 run_approval，且带决策人
    async with SessionLocal() as db:
        rows = await run_approval_repo.list_by_run(db, run_id)
    assert len(rows) == 1, f"应有一条审批记录，实际 {len(rows)} 条"
    assert rows[0].status == "approved"
    assert rows[0].comment == "可以执行"
    assert rows[0].decided_by == "tester"

    # 审批意见进入下游节点的输入
    write_call = llm.calls[calls_before + 1]
    prompt = "".join(
        m.get("content") or ""
        for m in write_call.get("messages", [])
        if isinstance(m.get("content"), str)
    )
    assert "可以执行" in prompt, f"下游没拿到审批意见：{prompt[:200]}"


async def test_reject_terminates_the_run(client: httpx.AsyncClient, llm: FakeLLM):
    """驳回 → 本次 run 就此终止，下游节点不跑。"""
    tag = f"rj{int(time.time() * 1000) % 1000000}"
    await _make_agent(client, f"{tag}_plan", llm)
    await _make_agent(client, f"{tag}_write", llm)
    await _make_workflow(
        client,
        f"{tag}_wf",
        _definition(f"{tag}_plan", f"{tag}_write"),
    )

    run_id = await _start_run(client, f"{tag}_wf")
    calls_before = len(llm.calls)
    ticket = await _ticket(client)

    async def on_approval(payload: dict) -> None:
        """收到审批请求：驳回。"""
        await _wait_status(client, run_id, RunStatus.WAITING.value)
        r = await _decide(client, run_id, False, "数据来源不可靠")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rejected"

    events = await _read_sse(
        client,
        f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0",
        on_approval=on_approval,
    )

    kinds = [e["event"] for e in events]
    assert "rejected" in kinds, f"没收到驳回事件：{kinds}"
    assert kinds[-1] == "done", f"流没以 done 收尾：{kinds}"

    done = events[-1]
    assert done.get("status") == RunStatus.REJECTED.value, done
    assert "数据来源不可靠" in (done.get("message") or ""), done

    # 驳回后不能再有待审批项
    pending = await client.get(f"/api/runs/{run_id}/approval")
    assert pending.status_code == 200
    assert pending.json() is None, "驳回后不该还挂着待审批项"

    await _wait_status(client, run_id, RunStatus.REJECTED.value)

    async with SessionLocal() as db:
        run = await run_repo.get_by_id(db, run_id)
    assert run is not None
    assert run.status == RunStatus.REJECTED.value
    assert "数据来源不可靠" in (run.error_msg or ""), run.error_msg

    # 下游的「撰写」节点没跑：只有规划那一次调用
    assert len(llm.calls) - calls_before == 1, (
        f"驳回后不应再调模型，实际调了 {len(llm.calls) - calls_before} 次"
    )
    assert "write" not in [e.get("node") for e in events if e["event"] == "node_start"]


async def test_double_decision_is_rejected(client: httpx.AsyncClient, llm: FakeLLM):
    """同一个审批只能决策一次——并发点两次「批准」不许都生效。"""
    tag = f"dd{int(time.time() * 1000) % 1000000}"
    await _make_agent(client, f"{tag}_plan", llm)
    await _make_agent(client, f"{tag}_write", llm)
    await _make_workflow(
        client, f"{tag}_wf", _definition(f"{tag}_plan", f"{tag}_write")
    )

    run_id = await _start_run(client, f"{tag}_wf")
    ticket = await _ticket(client)

    async def on_approval(payload: dict) -> None:
        """批准一次，再点一次——第二次必须被拒。"""
        await _wait_status(client, run_id, RunStatus.WAITING.value)
        first = await _decide(client, run_id, True, "第一次")
        assert first.status_code == 200, first.text
        second = await _decide(client, run_id, True, "第二次")
        assert second.status_code == 409, f"重复决策应 409，实际 {second.status_code}"

    events = await _read_sse(
        client,
        f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0",
        on_approval=on_approval,
    )
    assert events[-1]["event"] == "done"
    assert events[-1].get("status") == RunStatus.DONE.value


async def test_decision_on_non_waiting_run_conflicts(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """run 不在等待审批时提交决策 → 409，别让人以为能插队。"""
    tag = f"nw{int(time.time() * 1000) % 1000000}"
    await _make_agent(client, f"{tag}_plan", llm)
    await _make_agent(client, f"{tag}_write", llm)
    await _make_workflow(
        client, f"{tag}_wf", _definition(f"{tag}_plan", f"{tag}_write")
    )

    run_id = await _start_run(client, f"{tag}_wf")
    # 刚创建时 run 还在 pending/running，不是 waiting
    r = await _decide(client, run_id, True, "插队")
    assert r.status_code == 409, f"应 409，实际 {r.status_code}：{r.text}"


async def test_approval_endpoint_returns_null_when_nothing_pending(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """没有待审批项时 GET 返回 null（HTTP 200 + body null），前端据此收起卡片。"""
    tag = f"nl{int(time.time() * 1000) % 1000000}"
    await _make_agent(client, f"{tag}_a", llm)
    r = await client.post(
        "/api/workflows",
        json={
            "code": f"{tag}_wf",
            "name": "无审批",
            "definition": {
                "entry": "only",
                "nodes": [{"id": "only", "agentCode": f"{tag}_a", "outputKey": "out"}],
                "edges": [],
                "inputTemplate": "{{user_input}}",
            },
        },
    )
    assert r.status_code == 201, r.text

    run_id = await _start_run(client, f"{tag}_wf")
    ticket = await _ticket(client)
    events = await _read_sse(
        client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0"
    )
    assert events[-1]["event"] == "done"

    pending = await client.get(f"/api/runs/{run_id}/approval")
    assert pending.status_code == 200
    assert pending.json() is None


async def test_workflow_with_broken_approval_node_is_refused(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """配置期就把形状不对的审批节点拒掉，别等到跑起来才炸。"""
    tag = f"bk{int(time.time() * 1000) % 1000000}"
    await _make_agent(client, f"{tag}_a", llm)

    # 审批节点缺 prompt：审批人看什么？
    r = await client.post(
        "/api/workflows",
        json={
            "code": f"{tag}_wf1",
            "name": "缺 prompt",
            "definition": {
                "entry": "gate",
                "nodes": [{"id": "gate", "type": "approval"}],
                "edges": [],
            },
        },
    )
    assert r.status_code == 400, f"应 400，实际 {r.status_code}：{r.text}"
    assert "prompt" in r.text, r.text

    # 审批节点混了 agentCode：两套语义说不清谁生效
    r = await client.post(
        "/api/workflows",
        json={
            "code": f"{tag}_wf2",
            "name": "混 agentCode",
            "definition": {
                "entry": "gate",
                "nodes": [
                    {
                        "id": "gate",
                        "type": "approval",
                        "prompt": "批？",
                        "agentCode": f"{tag}_a",
                    }
                ],
                "edges": [],
            },
        },
    )
    assert r.status_code == 400, f"应 400，实际 {r.status_code}：{r.text}"
    assert "agentCode" in r.text, r.text
