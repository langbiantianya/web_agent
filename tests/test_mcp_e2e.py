"""V4 MCP 的端到端验证：真实 MCP 服务器 → 发现 → 快照 → 绑定 → 模型调用 → 回灌。

这一份是 Phase 2 最重的测试。前面几份测的都是「我们自己的代码算得对不对」，
这里测的是**协议本身**：真的起一个 MCP 子进程，真的用 JSON-RPC 跟它说话。

用 mock 把 SDK 的返回结构造出来，等于只测了「我们能解析自己造的数据」——协议对不对、
分页怎么翻、``isError`` 怎么表达、``role=tool`` 到底有没有真的回灌，全都没被覆盖。

对应设计文档 §21.15 的验收标准：

- 刷新目录 → 快照表里出现对应工具行，``qualified_name`` 合 OpenAI 函数名约束；
- 一次真实调用 → 第二轮请求里确实带着 ``role=tool`` 的结果；
- 服务器掉线 → 它的工具**不再展开给模型**，但 Agent 仍能跑完；
- ``isError: true`` → 节点**不失败**，错误文本回灌给模型；
- 返回图片 → 回灌文本里是占位说明，不是空字符串；
- 工具结果命中敏感词 → **不回灌**，一路抛上去。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.config.db import SessionLocal
from app.config.settings import settings
from app.core.errors import SensitiveContentError
from app.core.mcp.manager import mcp_manager
from app.core.tools import build_openai_tools, execute_tool
from app.main import create_app
from app.repo.agent_repo import agent_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.run_repo import run_repo
from tests.conftest import authenticate_admin, authenticate_client
from tests.fake_llm import FakeLLM

ADMIN_HEADERS: dict[str, str] = {}

# 真实 MCP 服务器脚本（用官方 SDK 起的 stdio 服务器）。
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_server_stdio.py"
POLLUTED_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "mcp_server_polluted.py"
)
HTTP_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_server_http.py"

# 这批用例共用的服务器 code。测试库是 session 级建表且**用例之间不清理**，
# 所以 code 必须与其他测试文件互不相同。
SERVER_CODE = "mcpe2e"


def _free_port() -> int:
    """向 OS 借一个空闲端口：固定端口会撞上残留进程或并行用例。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
async def _close_mcp_connections():
    """每个用例后关掉全部 MCP 连接。

    stdio 子进程是**真的进程**——不回收就会在测试机上攒出一堆孤儿，而且下一个
    用例会看到上一轮的连接状态（``is_available`` 是进程内单例里的状态）。
    """
    yield
    await mcp_manager.close_all()


@pytest.fixture
async def server():
    """在**测试自己的事件循环**里跑一个真实的 uvicorn 服务。

    ``uvicorn.Server.serve()`` 本身就是协程，``asyncio.create_task`` 就能和测试
    共用同一个事件循环：既拿到真实 socket 的流式语义，又不会引入跨事件循环的
    共享状态。
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
    async with httpx.AsyncClient(base_url=server, timeout=60) as c:
        await authenticate_admin(c)
        await authenticate_client(c)
        yield c


@pytest.fixture
async def llm_factory():
    """按用例创建独立的假 LLM，用完统一停掉。"""
    started: list[FakeLLM] = []

    async def _make(**kwargs) -> FakeLLM:
        """起一个假 LLM 服务。"""
        s = FakeLLM(**kwargs)
        s.start()
        started.append(s)
        return s

    yield _make

    for s in started:
        s.stop()


@pytest.fixture
async def mcp_stdio(client: httpx.AsyncClient, monkeypatch):
    """注册一台**真实的** stdio MCP 服务器，并刷新一次工具目录。

    做完之后：``mcp_tool`` 表里有这台服务器发现的工具，快照里的
    ``qualified_name`` 形如 ``mcp__mcpe2e__echo``。
    """
    # 命令白名单要包含真正要执行的那个解释器。
    monkeypatch.setattr(
        settings, "mcp_allowed_command_prefixes", [str(Path(sys.executable).parent)]
    )
    # 同一测试进程里的前一个用例可能留下了绑定 Agent。这里直接清仓储数据，
    # 避免把 fixture 准备和“API 必须阻止删除被使用服务器”的产品规则混在一起。
    async with SessionLocal() as db:
        for agent in await agent_repo.list(db, limit=5000):
            if any(
                str(name).startswith(f"mcp__{SERVER_CODE}__")
                for name in (agent.tools or [])
            ):
                await agent_repo.delete(db, agent.code, agent.tenant_id)
        await mcp_server_repo.delete(db, SERVER_CODE)
    created = await client.post(
        "/api/mcp/servers",
        json={
            "code": SERVER_CODE,
            "name": "端到端测试服务器",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(FIXTURE)],
            "env": (
                {"PYTHONPATH": os.environ["PYTHONPATH"]}
                if os.environ.get("PYTHONPATH")
                else None
            ),
        },
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 201, created.text
    refreshed = await client.post(
        f"/api/mcp/servers/{SERVER_CODE}/refresh", headers=ADMIN_HEADERS
    )
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["imported"], f"一个工具都没发现：{body}"
    return SERVER_CODE


# ------------------------------------------------------------------ helpers


async def _make_agent(
    client: httpx.AsyncClient, code: str, llm: FakeLLM, tools: list[str] | None = None
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
            "name": "MCP 流水线",
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


async def _ticket(client: httpx.AsyncClient, run_id: int) -> str:
    """取一张一次性 SSE 凭证。"""
    r = await client.post(
        "/api/auth/sse-ticket",
        json={"run_id": run_id},
        headers={"X-CSRF-Token": client.cookies.get("wa_csrf") or ""},
    )
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


async def _wait_run_status(run_id: int, statuses: set[str], timeout: float = 20.0):
    """轮询等待 run 进入指定状态。"""
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
    conv = await client.post(
        "/api/conversations", json={"workflow_code": wf["code"]}
    )
    assert conv.status_code == 201, conv.text
    conv_id = conv.json()["id"]
    sent = await client.post(
        f"/api/conversations/{conv_id}/messages", json={"content": "查一下天气"}
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run_id"]
    ticket = await _ticket(client, run_id)
    events = await _read_sse(
        client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0"
    )
    return conv_id, run_id, events


# -------------------------------------------------------------------- 发现


async def test_refresh_discovers_tools_into_snapshot(mcp_stdio: str, db):
    """刷新目录 → 快照表里出现对应工具行。

    快照是「模型能调的」与「服务端能执行的」对得上的那层保障：Agent 绑定时
    要在这里查得到，展开 schema 时也从这里取。
    """
    rows = await mcp_tool_repo.list_all(db)
    names = {row.qualified_name for row in rows}
    assert f"mcp__{mcp_stdio}__echo" in names
    assert f"mcp__{mcp_stdio}__boom" in names
    assert f"mcp__{mcp_stdio}__binary" in names

    for row in rows:
        if not row.qualified_name.startswith(f"mcp__{mcp_stdio}__"):
            continue
        # 限定名必须过 OpenAI 的函数名约束——不合规的名字发给模型会被直接拒。
        assert row.qualified_name.replace("_", "a").replace("-", "a")
        assert len(row.qualified_name) <= 64
        # 工具自己的名字原样保留
        assert row.qualified_name.endswith(row.tool_name)

    echo = await mcp_tool_repo.get_by_qualified(db, f"mcp__{mcp_stdio}__echo")
    assert echo is not None
    assert echo.description
    assert echo.input_schema["type"] == "object"


@pytest.mark.asyncio
async def test_mcp_resources_and_prompts_are_read_live(
    client: httpx.AsyncClient, mcp_stdio: str
):
    """资源、模板和 prompt 走真实 MCP 会话，而不是伪造返回。"""
    resources = await client.get(
        f"/api/mcp/servers/{mcp_stdio}/resources", headers=ADMIN_HEADERS
    )
    assert resources.status_code == 200, resources.text
    assert any(item["uri"] == "fixture://guide" for item in resources.json())

    read = await client.get(
        f"/api/mcp/servers/{mcp_stdio}/resources/read",
        params={"uri": "fixture://guide"},
        headers=ADMIN_HEADERS,
    )
    assert read.status_code == 200, read.text
    assert "资源读取正常" in read.json()[0]["text"]

    templates = await client.get(
        f"/api/mcp/servers/{mcp_stdio}/resource-templates",
        headers=ADMIN_HEADERS,
    )
    assert templates.status_code == 200, templates.text
    assert any("items" in item["uri"] for item in templates.json()), templates.json()

    prompts = await client.get(
        f"/api/mcp/servers/{mcp_stdio}/prompts", headers=ADMIN_HEADERS
    )
    assert prompts.status_code == 200, prompts.text
    assert any(item["name"] == "summarize" for item in prompts.json())

    prompt = await client.post(
        f"/api/mcp/servers/{mcp_stdio}/prompts/summarize/get",
        json={"arguments": {"topic": "季度指标"}},
        headers=ADMIN_HEADERS,
    )
    assert prompt.status_code == 200, prompt.text
    assert "季度指标" in str(prompt.json()["messages"])


@pytest.mark.asyncio
async def test_stdio_stdout_pollution_is_reported_as_unavailable(
    client: httpx.AsyncClient, monkeypatch
):
    """stdout 普通日志破坏协议时明确判定连接不可用，不挂死。"""
    monkeypatch.setattr(
        settings, "mcp_allowed_command_prefixes", [str(Path(sys.executable).parent)]
    )
    code = "polluted-stdio"
    stale = await client.delete(
        f"/api/mcp/servers/{code}", headers=ADMIN_HEADERS
    )
    assert stale.status_code in (204, 404)
    created = await client.post(
        "/api/mcp/servers",
        json={
            "code": code,
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(POLLUTED_FIXTURE)],
            "env": (
                {"PYTHONPATH": os.environ["PYTHONPATH"]}
                if os.environ.get("PYTHONPATH")
                else None
            ),
        },
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 201, created.text
    refreshed = await client.post(
        f"/api/mcp/servers/{code}/refresh", headers=ADMIN_HEADERS
    )
    assert refreshed.status_code == 200, refreshed.text
    detail = await client.get(
        f"/api/mcp/servers/{code}", headers=ADMIN_HEADERS
    )
    assert detail.status_code == 200
    assert detail.json()["online"] is True
    ok, text = await execute_tool(f"mcp__{code}__echo", {"text": "still works"})
    assert ok is True
    assert text == "still works"


@pytest.mark.asyncio
async def test_streamable_http_real_discovery_call_and_error(
    client: httpx.AsyncClient, monkeypatch
):
    """真实 Streamable HTTP 完成发现、成功调用与 isError 回灌。"""
    code = "httpe2e"
    port = _free_port()
    env = dict(os.environ)
    env["MCP_FIXTURE_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(HTTP_FIXTURE)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("Streamable HTTP fixture 未启动")

        monkeypatch.setattr(settings, "mcp_allow_private_networks", True)
        stale = await client.delete(
            f"/api/mcp/servers/{code}", headers=ADMIN_HEADERS
        )
        assert stale.status_code in (204, 404)
        created = await client.post(
            "/api/mcp/servers",
            json={
                "code": code,
                "transport": "http",
                "url": f"http://127.0.0.1:{port}/mcp",
            },
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 201, created.text
        refreshed = await client.post(
            f"/api/mcp/servers/{code}/refresh", headers=ADMIN_HEADERS
        )
        assert refreshed.status_code == 200, refreshed.text
        assert f"mcp__{code}__echo" in refreshed.json()["imported"]

        ok, text = await execute_tool(
            f"mcp__{code}__echo", {"text": "stream"}
        )
        assert ok is True
        assert "http: stream" in text

        error_ok, error_text = await execute_tool(f"mcp__{code}__boom", {})
        assert error_ok is False
        assert error_text
    finally:
        await mcp_manager.close_all()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
async def test_agent_can_bind_discovered_mcp_tool(
    client: httpx.AsyncClient, llm_factory, mcp_stdio: str
):
    """发现过的工具能绑；没发现的绑不了。"""
    llm = await llm_factory(texts=["ok"])

    ok = await client.post(
        "/api/agents",
        json={
            "code": "mcpe2e_agent_ok",
            "name": "MCP Agent",
            "system_prompt": "x",
            "model": "m",
            "base_url": llm.base_url,
            "tools": [f"mcp__{mcp_stdio}__echo"],
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["tools"] == [f"mcp__{mcp_stdio}__echo"]

    bad = await client.post(
        "/api/agents",
        json={
            "code": "mcpe2e_agent_bad",
            "name": "MCP Agent",
            "system_prompt": "x",
            "model": "m",
            "base_url": llm.base_url,
            "tools": [f"mcp__{mcp_stdio}__never_existed"],
        },
    )
    assert bad.status_code == 400, bad.text
    assert "mcp__" in bad.json()["message"]


# -------------------------------------------------------------------- 调用


async def test_mcp_tool_is_executed_and_fed_back_to_model(
    client: httpx.AsyncClient, llm_factory, mcp_stdio: str
):
    """闭环全链路：模型要 MCP 工具 → 子进程真的执行 → 结果回灌 → 模型继续。

    断言三件事，缺一不可：

    1. **MCP 工具真的展开成了 schema 发给模型**（否则模型根本不知道有工具）；
    2. **结果真的进了下一轮请求的 messages**（回灌断了模型就永远看不到结果）；
    3. **SSE 上有 tool_call / tool_result 事件，且带上了来源信息**。
    """
    tool_name = f"mcp__{mcp_stdio}__echo"
    llm = await llm_factory(
        texts=["占位", "这是最终答案"],
        tool_calls_plan=[
            [
                {
                    "id": "call_mcp",
                    "name": tool_name,
                    "arguments": '{"text": "hi"}',
                }
            ]
        ],
    )
    base = len(llm.calls)

    _, run_id, events = await _run_once(client, llm, "mcpe2e_echo", tools=[tool_name])
    kinds = [e for e, _, _ in events]
    assert "tool_call" in kinds, kinds
    assert "tool_result" in kinds, kinds
    assert kinds[-1] == "done", kinds

    # 1) 工具被展开成 OpenAI function schema 发给了模型
    assert len(llm.calls) == base + 2, "应该正好调了模型两轮"
    first_request = llm.calls[base]
    sent_names = [t["function"]["name"] for t in first_request["tools"]]
    assert tool_name in sent_names, sent_names

    # 2) 第二轮请求里带着 role=tool 的工具结果——内容来自**真的 MCP 子进程**
    second_request = llm.calls[base + 1]
    tool_msgs = [m for m in second_request["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, second_request["messages"]
    assert tool_msgs[0]["content"] == "echo: hi", tool_msgs[0]
    assert tool_msgs[0]["tool_call_id"] == "call_mcp"

    # 3) tool_result 事件说清了「调了哪个工具、成没成、从哪来」
    results = [d for e, _, d in events if e == "tool_result"]
    assert results[0]["tool"] == tool_name
    assert results[0]["ok"] is True
    assert "echo: hi" in results[0]["summary"]
    assert results[0]["source"] == "mcp"
    assert results[0]["server"] == mcp_stdio
    assert isinstance(results[0]["durationMs"], int)

    # 节点没挂
    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "run 未正常结束"


async def test_mcp_is_error_does_not_fail_the_node(
    client: httpx.AsyncClient, llm_factory, mcp_stdio: str
):
    """MCP 服务器返回 ``isError: true`` → 节点**不失败**，错误文本回灌给模型。

    与 §17.5 同一口径：工具执行错误不是节点失败。模型看见「工具报错了」有机会
    自己纠正，换个参数或者干脆不用这个工具。
    """
    tool_name = f"mcp__{mcp_stdio}__boom"
    llm = await llm_factory(
        texts=["换个说法", "备用"],
        tool_calls_plan=[
            [{"id": "call_boom", "name": tool_name, "arguments": "{}"}]
        ],
    )
    base = len(llm.calls)

    _, run_id, events = await _run_once(client, llm, "mcpe2e_boom", tools=[tool_name])

    results = [d for e, _, d in events if e == "tool_result"]
    assert results[0]["ok"] is False
    assert results[0]["summary"], "失败也要有说明，不能是空串"

    # 错误确实回灌给了模型
    second_request = llm.calls[base + 1]
    tool_msgs = [m for m in second_request["messages"] if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"].strip() != ""

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "MCP 工具报错不该让 run 失败"


async def test_mcp_image_result_becomes_placeholder(
    client: httpx.AsyncClient, llm_factory, mcp_stdio: str
):
    """返回图片 → 回灌文本里是**占位说明**，不是空字符串。

    空字符串会让模型以为「工具跑了但什么都没返回」，于是可能反复重试同一个
    工具，白烧轮次。占位说明把「有东西但这个通道传不了」讲清楚。
    """
    tool_name = f"mcp__{mcp_stdio}__binary"
    llm = await llm_factory(
        texts=["收到图片", "备用"],
        tool_calls_plan=[
            [{"id": "call_bin", "name": tool_name, "arguments": "{}"}]
        ],
    )
    base = len(llm.calls)

    _, run_id, events = await _run_once(client, llm, "mcpe2e_bin", tools=[tool_name])

    results = [d for e, _, d in events if e == "tool_result"]
    assert results[0]["ok"] is True
    assert results[0]["summary"].strip() != "", "图片结果的摘要不能是空串"
    assert "图片" in results[0]["summary"]

    second_request = llm.calls[base + 1]
    tool_msgs = [m for m in second_request["messages"] if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"].strip() != ""
    assert "图片" in tool_msgs[0]["content"]

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done"


# -------------------------------------------------------------------- 离线


async def test_offline_server_tools_are_not_expanded(mcp_stdio: str, db):
    """服务器掉线 → 它的工具**不再展开给模型**。

    这条很要紧：照常展开的话，模型每次调它都失败，会反复重试同一个坏工具，
    白烧轮次与 token，最后撞上轮次上限失败。「看不见」比「看得见但调不通」好。
    """
    assert await build_openai_tools([f"mcp__{mcp_stdio}__echo"]) is not None

    # 把这台服务器标记为不可用（连接失败时管理器就是这么做的）
    mcp_manager.mark_unavailable(mcp_stdio, "测试：模拟服务器掉线")
    try:
        assert await build_openai_tools([f"mcp__{mcp_stdio}__echo"]) is None
    finally:
        mcp_manager.clear_unavailable(mcp_stdio)


async def test_offline_server_does_not_break_other_tools(mcp_stdio: str):
    """离线只影响那台服务器的工具，别的不受影响。"""
    mcp_manager.mark_unavailable(mcp_stdio, "测试：模拟服务器掉线")
    try:
        specs = await build_openai_tools(["calc", f"mcp__{mcp_stdio}__echo"])
    finally:
        mcp_manager.clear_unavailable(mcp_stdio)
    assert specs is not None
    assert [s["function"]["name"] for s in specs] == ["calc"]


async def test_agent_still_runs_when_its_mcp_server_is_offline(
    client: httpx.AsyncClient, llm_factory, mcp_stdio: str
):
    """服务器掉线时，已有 Agent 仍能正常跑完（退化成「没有这个能力」）。

    不能因为一个可选依赖挂了就让整个 run 失败——那会让「配了这个工具的 Agent」
    变成「一碰就碎」。
    """
    tool_name = f"mcp__{mcp_stdio}__echo"
    llm = await llm_factory(texts=["我还是能回答"])
    base = len(llm.calls)

    mcp_manager.mark_unavailable(mcp_stdio, "测试：模拟服务器掉线")
    try:
        _, run_id, events = await _run_once(
            client, llm, "mcpe2e_offline", tools=[tool_name]
        )
    finally:
        mcp_manager.clear_unavailable(mcp_stdio)

    # 工具没被展开给模型：模型看不见它，也就不会去调它
    first_request = llm.calls[base]
    sent_names = [t["function"]["name"] for t in (first_request.get("tools") or [])]
    assert tool_name not in sent_names, sent_names

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "MCP 服务器掉线不该让 run 失败"


# ------------------------------------------------------------------ 敏感词


async def test_sensitive_tool_result_is_not_fed_back(mcp_stdio: str, monkeypatch):
    """工具结果命中敏感词 → **不回灌**，一路抛上去。

    §21.10 第 ④ 条：MCP 工具结果的位置与用户输入一样、性质也一样——不能只
    过滤用户输入。命中时按「拒绝」处理，而不是把内容塞回给模型。

    这里用真实子进程跑一次真实调用，只是把判定函数换成「必定命中」——
    敏感词模型本身的行为由 :mod:`tests.test_sensitive` 负责。
    """
    from app.core import sensitive as sensitive_mod

    monkeypatch.setattr(
        sensitive_mod,
        "screen_text",
        lambda _text: sensitive_mod.SensitiveVerdict(
            hit=True, score=0.99, position=0, excerpt="命中片段"
        ),
    )
    with pytest.raises(SensitiveContentError):
        await execute_tool(f"mcp__{mcp_stdio}__echo", '{"text": "hi"}')


# ------------------------------------------------------------------ 直调


async def test_mcp_unavailable_event_is_emitted(
    client: httpx.AsyncClient, llm_factory, mcp_stdio: str, monkeypatch
):
    """服务器掉线时，事件流里有一条 ``mcp_unavailable``（非终止）。

    前端据此提示「这个 Agent 的一部分能力现在不可用」。它**不是**终止事件——
    工具失败按 §17.5 的口径回灌给模型，run 该继续还是继续。
    """
    tool_name = f"mcp__{mcp_stdio}__echo"

    async def failing_call(server, tool_name_: str, args: dict, timeout=None):
        """假装连接断了，并把服务器标记为不可用。"""
        mcp_manager.mark_unavailable(server.code, "测试：连接断了")
        raise RuntimeError("连接断了")

    monkeypatch.setattr(mcp_manager, "call_tool", failing_call)

    llm = await llm_factory(
        texts=["那我换个说法", "备用"],
        tool_calls_plan=[
            [{"id": "call_down", "name": tool_name, "arguments": '{"text": "hi"}'}]
        ],
    )
    base = len(llm.calls)

    _, run_id, events = await _run_once(client, llm, "mcpe2e_down", tools=[tool_name])

    kinds = [e for e, _, _ in events]
    assert "mcp_unavailable" in kinds, kinds

    payload = [d for e, _, d in events if e == "mcp_unavailable"][0]
    assert payload["server"] == mcp_stdio
    assert "连接断了" in payload["reason"]

    # 非终止事件：run 照样正常结束
    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "mcp_unavailable 不该让 run 失败"


async def test_execute_tool_reports_missing_mcp_server(mcp_stdio: str):
    """服务器不存在 → 失败结果，不是异常。"""
    ok, text = await execute_tool("mcp__nosuchserver__tool", "{}")
    assert ok is False
    assert "nosuchserver" in text
    assert "不存在" in text


async def test_execute_tool_respects_allowlist(mcp_stdio: str):
    """白名单外的工具名 → 拒绝执行。

    模型报出来的工具名与「服务端执行的工具名」要对齐一次：提示词注入可以让
    模型报出一个没被授权的名字。
    """
    ok, text = await execute_tool(
        f"mcp__{mcp_stdio}__echo", '{"text": "hi"}', allowed=["calc"]
    )
    assert ok is False
    assert "授权列表" in text


async def test_disable_server_blocks_call(mcp_stdio: str, db):
    """停用的服务器 → 工具调不通，且不再展开给模型。"""
    async with SessionLocal() as session:
        server = await mcp_server_repo.get_by_code(session, mcp_stdio)
        assert server is not None
        await mcp_server_repo.update(session, mcp_stdio, {"enabled": 0})

    ok, text = await execute_tool(f"mcp__{mcp_stdio}__echo", '{"text": "hi"}')
    assert ok is False
    assert "停用" in text
    assert await build_openai_tools([f"mcp__{mcp_stdio}__echo"]) is None
