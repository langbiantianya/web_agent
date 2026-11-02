"""Phase 3 的端到端验证：真 studio + 真 uvicorn + 假 LLM（设计文档 §22.13）。

## 为什么这一层不可省

前面几层各自都钉住了一部分事实：

- :mod:`tests.test_skill_exec` —— 校验与参数规整（**没发请求**）；
- :mod:`tests.test_studio_client` —— 客户端与 gRPC 错误码翻译；
- :mod:`tests.test_studio_service` —— 服务端执行、超时、截断、去重。

但 §22.13 的验收项里有几条**只有把两边接起来**才成立：

1. **脚本真的在 studio 那边跑**——主服务进程内不起任何子进程；
2. **脚本的输出真的回灌进了模型的下一轮请求**；
3. **studio 挂掉时，工具失败但 run 不失败**（§17.5）；
4. **校验不过的请求根本没发出去**。

第 4 条尤其需要「真 studio」才能证伪：光看客户端的返回值，区分不了
「studio 拒了」和「压根没发」。

所以这里起的是**真的** studio（同一个进程内的 gRPC 服务 + 真的子进程）、
**真的** uvicorn（不是 ASGITransport——SSE 需要真 socket 的流式语义），
只有 LLM 是假的（:mod:`tests.fake_llm`）。
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass, field

import grpc
import httpx
import pytest
import uvicorn

from app.config.db import SessionLocal
from app.config.settings import settings
from app.main import create_app
from app.repo.run_repo import run_repo
from services.studio.config import StudioConfig
from services.studio.server import StudioService
from tests.fake_llm import FakeLLM
from webagent.studio.v1 import studio_pb2_grpc

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key"}

# 本文件所有 code 都带这个前缀：pytest 的库是 session 级建表、用例之间**不清理**，
# 撞 code 会变成「上一个用例的残留」。
PREFIX = "stdeo"


def _free_port() -> int:
    """向 OS 借一个空闲端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ studio


class _CountingStudio(StudioService):
    """记录 ``Exec`` 调用次数的 studio。

    「请求根本没发出去」这条验收项要靠它证伪：调用次数没涨，就说明请求连
    studio 的门都没进。
    """

    def __init__(self, config: StudioConfig) -> None:
        """建好父类的闸门与执行器，再把计数器归零。

        Args:
            config: 服务配置。
        """
        super().__init__(config)
        self.exec_calls = 0

    async def Exec(self, request, context):  # noqa: N802 - gRPC 的命名
        """计数后转交父类。"""
        self.exec_calls += 1
        return await super().Exec(request, context)


@dataclass
class _StudioHandle:
    """测试用的 studio 句柄。"""

    addr: str
    service: _CountingStudio
    server: grpc.aio.Server
    stopped: bool = False

    async def stop(self) -> None:
        """把 studio 关掉——用来模拟「studio 挂了」。"""
        if self.stopped:
            return
        self.stopped = True
        await self.server.stop(grace=0.5)


@pytest.fixture
async def studio():
    """在**测试自己的事件循环**里跑一个真实的 studio gRPC 服务。

    刻意与 web_agent 的 uvicorn 同一个事件循环：studio 是 grpc.aio 服务，跨
    循环共享 channel/stub 会踩到「Future attached to a different loop」这类
    说不清的错。
    """
    port = _free_port()
    config = StudioConfig(
        host="127.0.0.1",
        port=port,
        # 并发只有 1 位、等待队列深 0：**任何一个并发请求都会被立刻拒**。
        # 本文件的用例都是串行调用，这个配置对它们没有影响；但它让「队列满」
        # 那条故障注入可以稳定复现——并发位被占住时，下一个请求一定拿到
        # RESOURCE_EXHAUSTED，而不是「排在队里等着」。
        max_concurrency=1,
        max_queue=0,
        # 服务端的时限上限。给足余量，让「超时」只可能来自模型请求的 timeout_ms。
        max_timeout_ms=30_000,
        max_stdout_bytes=8 * 1024,
        max_stderr_bytes=8 * 1024,
    )
    service = _CountingStudio(config)
    server = grpc.aio.server()
    studio_pb2_grpc.add_StudioServicer_to_server(service, server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    handle = _StudioHandle(
        addr=f"127.0.0.1:{port}", service=service, server=server
    )
    try:
        yield handle
    finally:
        await handle.stop()


# ------------------------------------------------------------------ 应用


@pytest.fixture(autouse=True)
def _reset_registry():
    """每个用例后把可选工具恢复成「未注册」。

    ``skill_exec`` 是**可选工具**：studio 可用时才注册。这个 app fixture 会在
    启动期把它注册上，不复位的话会漏给后面的用例——「studio 不可用时
    skill_exec 不该存在」那条断言会被污染。
    """
    yield
    from app.core.tools import set_optional_tools_enabled

    set_optional_tools_enabled(False)


@pytest.fixture
async def app_server(studio, monkeypatch):
    """起一个**真的** uvicorn，指向**真的** studio。

    ``studio_exec_required=True``：studio 连不上就让服务起不来——这条由
    :mod:`tests.test_studio_startup` 单独钉住，这里要的是「能连上」那条路。
    """
    monkeypatch.setattr(settings, "studio_exec_addr", studio.addr)
    monkeypatch.setattr(settings, "studio_exec_required", True)
    monkeypatch.setattr(settings, "studio_exec_timeout_ms", 10_000)
    monkeypatch.setattr(settings, "studio_exec_max_timeout_ms", 30_000)

    port = _free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="error"
    )
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())

    deadline = time.time() + 20
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
async def client(app_server: str):
    """真实 HTTP 客户端（真 socket，不是 ASGITransport）。"""
    async with httpx.AsyncClient(base_url=app_server, timeout=60) as c:
        yield c


@pytest.fixture
async def llm_factory():
    """按用例创建独立的假 LLM。"""
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


async def _make_skill(
    client: httpx.AsyncClient, code: str, script: str, body: str
) -> str:
    """建一个技能并塞一个脚本资源。

    Args:
        client: HTTP 客户端。
        code: 技能 code。
        script: 脚本相对路径。
        body: 脚本正文。

    Returns:
        技能 code。
    """
    created = await client.post(
        "/api/skills",
        json={"code": code, "description": "端到端用技能", "body": "技能说明。"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 201, created.text
    uploaded = await client.post(
        f"/api/skills/{code}/resources",
        params={"path": script},
        content=body.encode("utf-8"),
        headers={**ADMIN_HEADERS, "Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 201, uploaded.text
    return code


async def _make_agent(
    client: httpx.AsyncClient,
    code: str,
    llm: FakeLLM,
    *,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
):
    """建一个指向假 LLM 的 Agent。

    Args:
        client: HTTP 客户端。
        code: Agent code。
        llm: 假 LLM。
        tools: 工具白名单。
        skills: 绑定的技能 code 列表。

    Returns:
        服务端返回的配置体。
    """
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
    if skills is not None:
        payload["skills"] = skills
    r = await client.post("/api/agents", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _run_once(
    client: httpx.AsyncClient, llm: FakeLLM, code: str
) -> tuple[int, int, list[tuple[str, int, dict]]]:
    """跑一个单节点流水线。

    Args:
        client: HTTP 客户端。
        llm: 假 LLM。
        code: Agent code。

    Returns:
        ``(会话 id, run id, SSE 事件列表)``。
    """
    wf = await client.post(
        "/api/workflows",
        json={
            "code": f"{PREFIX}_wf_{code}",
            "name": "脚本流水线",
            "definition": {
                "entry": code,
                "nodes": [{"id": code, "agentCode": code, "outputKey": "out"}],
                "edges": [],
                "inputTemplate": "问题：{{user_input}}",
            },
        },
    )
    assert wf.status_code == 201, wf.text
    conv = await client.post(
        "/api/conversations", json={"workflow_code": wf.json()["code"], "user_id": "u1"}
    )
    assert conv.status_code == 201, conv.text
    sent = await client.post(
        f"/api/conversations/{conv.json()['id']}/messages", json={"content": "跑一下"}
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run_id"]
    ticket = await client.post("/api/auth/sse-ticket", json={"user_id": "u1"})
    assert ticket.status_code == 200, ticket.text
    events = await _read_sse(
        client, f"/api/runs/{run_id}/stream?ticket={ticket.json()['ticket']}&last_seq=0"
    )
    return conv.json()["id"], run_id, events


async def _read_sse(client: httpx.AsyncClient, url: str) -> list[tuple[str, int, dict]]:
    """读取 SSE 流，返回 ``[(event, id, data), ...]``。"""
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


async def _wait_run_status(run_id: int, statuses: set[str], timeout: float = 30.0):
    """轮询等待 run 进入指定状态。

    每次轮询都开一个**新的短连接**并在读完立刻释放：跨 await 长时间持有读事务
    会阻塞 SQLite 的写者。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with SessionLocal() as s:
            run = await run_repo.get_by_id(s, run_id)
            if run is not None and run.status in statuses:
                return run
        await asyncio.sleep(0.02)
    return None


def _tool_results(events: list[tuple[str, int, dict]]) -> list[dict]:
    """挑出 tool_result 事件的载荷。"""
    return [d for e, _, d in events if e == "tool_result"]


def _tool_messages(llm: FakeLLM, base: int) -> list[dict]:
    """挑出第二次请求里 role=tool 的消息。"""
    second = llm.calls[base + 1]
    return [m for m in second["messages"] if m.get("role") == "tool"]


async def _hold_the_slot(studio: _StudioHandle, seconds: int = 5) -> None:
    """用一个真实的 gRPC 调用占住 studio 的并发位。

    直接用 proto 的桩发请求——这不是「绕过 web_agent」，而是**测试自己扮演一
    个别的调用方**，把 studio 占满，好让 web_agent 那边的请求被拒。

    Args:
        studio: studio 句柄。
        seconds: 占用时长（秒）。
    """
    from webagent.studio.v1 import studio_pb2

    channel = grpc.aio.insecure_channel(studio.addr)
    try:
        stub = studio_pb2_grpc.StudioStub(channel)
        await stub.Exec(
            studio_pb2.ExecRequest(
                skill="holder",
                script="scripts/hold.py",
                language="python",
                content=f"import time\ntime.sleep({seconds})\n",
                timeout_ms=30_000,
                request_id="stdeo-saturate-holder",
            ),
            timeout=60,
        )
    finally:
        await channel.close()


async def _wait_slot_busy(studio: _StudioHandle, timeout: float = 10.0) -> None:
    """等 studio 的并发位被占住。

    直接读服务端的信号量——这是唯一能确定「位已经满了」的依据，靠 sleep 猜
    会 flaky。

    Args:
        studio: studio 句柄。
        timeout: 超时（秒）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if studio.service._semaphore.locked():  # noqa: SLF001 - 测试要看真信号量
            return
        await asyncio.sleep(0.02)
    raise AssertionError("studio 的并发位一直没被占住")


# -------------------------------------------------------------------- 测试


async def test_skill_exec_runs_script_in_studio_and_feeds_model(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """全链路：模型要脚本 → studio 真跑 → 输出回灌 → 模型据此作答。

    断言四件事，缺一不可：

    1. **studio 真的收到并执行了请求**（``exec_calls`` 涨了）；
    2. **tool_result 事件里带回了脚本的真实输出**；
    3. **输出进了下一轮请求的 messages**——闭环的关键，回灌断了模型就永远
       看不到结果，只能瞎编；
    4. **run 正常结束**。
    """
    skill_code = f"{PREFIX}-hello"
    await _make_skill(
        client,
        skill_code,
        "scripts/hello.py",
        "import sys\nprint('hello from studio')\nprint('argv1=' + (sys.argv[1] if len(sys.argv) > 1 else 'none'))\n",
    )
    llm = await llm_factory(
        texts=["占位", "脚本说 hello from studio"],
        tool_calls_plan=[
            [
                {
                    "id": "call_exec_1",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {
                            "skill": skill_code,
                            "script": "scripts/hello.py",
                            "args": ["world"],
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        ],
    )
    base = len(llm.calls)

    await _make_agent(
        client, f"{PREFIX}_agent_ok", llm, tools=["skill_exec"], skills=[skill_code]
    )
    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_ok")

    # 1) studio 真的执行了
    assert studio.service.exec_calls == 1, studio.service.exec_calls

    kinds = [e for e, _, _ in events]
    assert "tool_call" in kinds, kinds
    assert "tool_result" in kinds, kinds

    results = _tool_results(events)
    assert len(results) == 1
    assert results[0]["tool"] == "skill_exec"
    assert results[0]["ok"] is True, results[0]
    # 2) 事件摘要里带回了脚本的真实输出
    assert "hello from studio" in results[0]["summary"], results[0]

    # 3) 输出进了下一轮请求
    tool_msgs = _tool_messages(llm, base)
    assert len(tool_msgs) == 1, tool_msgs
    assert "hello from studio" in tool_msgs[0]["content"], tool_msgs[0]
    assert tool_msgs[0]["tool_call_id"] == "call_exec_1"
    # 参数也真的传进去了（不经 shell）
    assert "argv1=world" in tool_msgs[0]["content"], tool_msgs[0]

    # 4) run 正常结束
    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "run 未正常结束"


async def test_nonzero_exit_is_fed_back_to_model_not_a_service_error(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """脚本自己 ``exit 3``：是**脚本失败**，不是 studio 故障。

    §22.8 的铁律：gRPC 成功 ≠ 脚本成功。调用方看到 ``exit_code=3`` + stderr，
    应该把它当成「脚本跑完了但失败了」；如果 studio 用 gRPC 错误码报「脚本失败」，
    调用方就只能当成「服务不可用」处理——语义完全错了，模型也拿不到 stderr。

    断言：工具结果 ``ok=False``、内容里有退出码与 stderr、**run 仍然正常结束**。
    """
    skill_code = f"{PREFIX}-fail"
    await _make_skill(
        client,
        skill_code,
        "scripts/fail.py",
        "import sys\nsys.stderr.write('boom happened\\n')\nsys.exit(3)\n",
    )
    llm = await llm_factory(
        texts=["占位", "脚本失败了"],
        tool_calls_plan=[
            [
                {
                    "id": "call_exec_fail",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {"skill": skill_code, "script": "scripts/fail.py"}
                    ),
                }
            ]
        ],
    )
    base = len(llm.calls)

    await _make_agent(
        client, f"{PREFIX}_agent_fail", llm, tools=["skill_exec"], skills=[skill_code]
    )
    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_fail")

    # gRPC 层是成功的：请求确实到了 studio，也确实被执行了
    assert studio.service.exec_calls == 1

    results = _tool_results(events)
    assert results[0]["ok"] is False, results[0]

    tool_msgs = _tool_messages(llm, base)
    assert tool_msgs, "错误结果必须回灌给模型"
    content = tool_msgs[0]["content"]
    assert "exit_code: 3" in content, content
    assert "boom happened" in content, content

    # 工具失败**不失败节点**：模型看到 stderr 还有机会自己纠正。
    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "工具出错不该让 run 失败"


async def test_script_timeout_is_reported_not_hung(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """脚本跑太久 → 超时被杀 → 状态回灌给模型。

    两道时限：studio 侧的自设时限（``timeout_ms``）与调用方的 gRPC deadline
    （时限 + 2 秒宽限）。这里让 studio 侧先到——它才是权威。
    """
    skill_code = f"{PREFIX}-slow"
    await _make_skill(
        client,
        skill_code,
        "scripts/slow.py",
        "import time\ntime.sleep(30)\nprint('never')\n",
    )
    llm = await llm_factory(
        texts=["占位", "脚本超时了"],
        tool_calls_plan=[
            [
                {
                    "id": "call_exec_slow",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {
                            "skill": skill_code,
                            "script": "scripts/slow.py",
                            "timeout_ms": 800,
                        }
                    ),
                }
            ]
        ],
    )
    base = len(llm.calls)

    await _make_agent(
        client, f"{PREFIX}_agent_slow", llm, tools=["skill_exec"], skills=[skill_code]
    )
    started = time.monotonic()
    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_slow")
    elapsed = time.monotonic() - started

    # 真的被杀掉了：没有等到脚本自己跑完（30 秒）
    assert elapsed < 20, f"等了 {elapsed:.1f} 秒才回来，说明超时没生效"

    results = _tool_results(events)
    assert results[0]["ok"] is False, results[0]
    tool_msgs = _tool_messages(llm, base)
    content = tool_msgs[0]["content"]
    assert "status: timeout" in content, content
    assert "超时" in content, content

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done"


async def test_output_over_limit_is_truncated(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """脚本输出超过上限 → 截断 + 标记，回灌给模型。

    studio 的 ``max_stdout_bytes`` 在这个 fixture 里是 8 KB，脚本打印 64 KB。
    """
    skill_code = f"{PREFIX}-loud"
    await _make_skill(
        client,
        skill_code,
        "scripts/loud.py",
        "print('x' * 65536)\n",
    )
    llm = await llm_factory(
        texts=["占位", "输出被截断了"],
        tool_calls_plan=[
            [
                {
                    "id": "call_exec_loud",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {"skill": skill_code, "script": "scripts/loud.py"}
                    ),
                }
            ]
        ],
    )
    base = len(llm.calls)

    await _make_agent(
        client, f"{PREFIX}_agent_loud", llm, tools=["skill_exec"], skills=[skill_code]
    )
    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_loud")

    results = _tool_results(events)
    assert results[0]["ok"] is False, results[0]
    tool_msgs = _tool_messages(llm, base)
    content = tool_msgs[0]["content"]
    assert "status: limited" in content, content
    assert "已截断" in content, content
    # 截断后的内容还在（模型最需要看到的就是它）
    assert "x" * 100 in content, content[:200]

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done"


async def test_studio_down_fails_tool_but_run_survives(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """studio 被 kill：工具失败，**run 不失败**（§17.5）。

    studio 挂掉是**服务级**故障，不是脚本的问题。回灌给模型一句「studio 连不
    上」，模型还能改用别的办法回答；把整个 run 判死则等于用户白等一场。
    """
    skill_code = f"{PREFIX}-down"
    await _make_skill(
        client,
        skill_code,
        "scripts/ok.py",
        "print('never reached')\n",
    )
    llm = await llm_factory(
        texts=["占位", "studio 连不上了"],
        tool_calls_plan=[
            [
                {
                    "id": "call_exec_down",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {"skill": skill_code, "script": "scripts/ok.py"}
                    ),
                }
            ]
        ],
    )
    base = len(llm.calls)

    await _make_agent(
        client, f"{PREFIX}_agent_down", llm, tools=["skill_exec"], skills=[skill_code]
    )
    # studio 先死掉
    await studio.stop()

    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_down")

    results = _tool_results(events)
    assert results[0]["ok"] is False, results[0]
    tool_msgs = _tool_messages(llm, base)
    content = tool_msgs[0]["content"]
    assert "连不上 studio" in content, content

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "studio 挂了不该让 run 失败"


async def test_rejected_request_never_reaches_studio(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """校验不过的请求**根本没发出去**（§22.13 的验收项）。

    三道校验全在 web_agent 侧：技能白名单、脚本路径格式、argv 形态。studio
    收到的永远是已校验过的内容——它压根不知道技能是什么。

    这里让模型依次要三种非法请求，断言 studio 的 ``exec_calls`` **一次都没涨**。
    """
    skill_code = f"{PREFIX}-guard"
    await _make_skill(
        client,
        skill_code,
        "scripts/ok.py",
        "print('never')\n",
    )
    # 另一个技能：存在，但**没绑到这个 Agent** 上。
    other_code = f"{PREFIX}-guard-other"
    await _make_skill(client, other_code, "scripts/ok.py", "print('never')\n")

    llm = await llm_factory(
        texts=["占位", "占位2", "占位3", "都被挡了"],
        tool_calls_plan=[
            [
                {
                    "id": "call_bad_path",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {"skill": skill_code, "script": "../../etc/passwd"}
                    ),
                }
            ],
            [
                {
                    "id": "call_bad_dir",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {"skill": skill_code, "script": "data/secret.py"}
                    ),
                }
            ],
            [
                {
                    "id": "call_bad_argv",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {
                            "skill": skill_code,
                            "script": "scripts/ok.py",
                            "args": ["../../etc/passwd"],
                        }
                    ),
                }
            ],
        ],
    )

    await _make_agent(
        client, f"{PREFIX}_agent_guard", llm, tools=["skill_exec"], skills=[skill_code]
    )
    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_guard")

    # 一个请求都没发出去
    assert studio.service.exec_calls == 0, studio.service.exec_calls

    results = _tool_results(events)
    assert len(results) == 3, results
    assert all(r["ok"] is False for r in results), results

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done"


async def test_skill_not_bound_to_agent_is_refused(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """没绑到 Agent 上的技能：连「它在不在」都不该告诉模型。

    白名单检查在**存在性检查之前**——否则模型可以靠报错信息探测出别的技能存
    不存在。而且请求同样**根本没发出去**。
    """
    skill_code = f"{PREFIX}-unbound"
    await _make_skill(client, skill_code, "scripts/ok.py", "print('never')\n")

    llm = await llm_factory(
        texts=["占位", "没有权限"],
        tool_calls_plan=[
            [
                {
                    "id": "call_unbound",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {"skill": skill_code, "script": "scripts/ok.py"}
                    ),
                }
            ]
        ],
    )

    await _make_agent(
        client,
        f"{PREFIX}_agent_unbound",
        llm,
        tools=["skill_exec"],
        skills=[],
    )
    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_unbound")

    assert studio.service.exec_calls == 0, studio.service.exec_calls
    results = _tool_results(events)
    assert results[0]["ok"] is False, results[0]
    assert "授权列表" in results[0]["summary"], results[0]

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done"


async def test_studio_saturated_is_reported_to_model(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """studio 忙不过来（队列满）→ 工具失败、**run 不失败**。

    复现方式：先用**测试自己**的 gRPC 连接占住 studio 唯一的并发位（跑一个
    睡 5 秒的脚本），等 web_agent 那边的请求打过来时，studio 已经满了。

    刻意不走「两个并行分支同时抢」那条路：并行与否取决于编排器与 LangGraph
    的调度时序，断言「谁被拒」会变成 flaky。用测试自己占位，拒的是**哪一边**
    就完全确定了。

    断言：gRPC 层是 ``RESOURCE_EXHAUSTED``，web_agent 把它翻译成「studio 忙不
    过来」回灌给模型，run 仍然正常结束。
    """
    skill_code = f"{PREFIX}-busy"
    await _make_skill(
        client, skill_code, "scripts/quick.py", "print('never reached')\n"
    )
    llm = await llm_factory(
        texts=["占位", "studio 忙不过来了"],
        tool_calls_plan=[
            [
                {
                    "id": "call_busy",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {"skill": skill_code, "script": "scripts/quick.py"}
                    ),
                }
            ]
        ],
    )

    await _make_agent(
        client, f"{PREFIX}_agent_busy", llm, tools=["skill_exec"], skills=[skill_code]
    )

    # 占住 studio 唯一的并发位。
    holder = asyncio.create_task(_hold_the_slot(studio))
    try:
        await _wait_slot_busy(studio)
        _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_busy")
    finally:
        holder.cancel()

    results = _tool_results(events)
    assert len(results) == 1, results
    assert results[0]["ok"] is False, results[0]
    assert "忙不过来" in results[0]["summary"], results[0]

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "studio 满了不该让 run 失败"


async def test_stdin_reaches_the_script(
    client: httpx.AsyncClient, llm_factory, studio: _StudioHandle
):
    """stdin 真的送进了脚本，并且脚本读到了 EOF。

    不关 stdin 的话，脚本会一直等输入直到超时——这条钉的是「传得进去**且**读得
    完」。
    """
    skill_code = f"{PREFIX}-stdin"
    await _make_skill(
        client,
        skill_code,
        "scripts/echo.py",
        "import sys\ndata = sys.stdin.read()\nprint('in:' + data)\n",
    )
    llm = await llm_factory(
        texts=["占位", "收到了"],
        tool_calls_plan=[
            [
                {
                    "id": "call_stdin",
                    "name": "skill_exec",
                    "arguments": json.dumps(
                        {
                            "skill": skill_code,
                            "script": "scripts/echo.py",
                            "stdin": "来自 stdin 的内容",
                        }
                    ),
                }
            ]
        ],
    )
    base = len(llm.calls)

    await _make_agent(
        client, f"{PREFIX}_agent_stdin", llm, tools=["skill_exec"], skills=[skill_code]
    )
    _, run_id, events = await _run_once(client, llm, f"{PREFIX}_agent_stdin")

    results = _tool_results(events)
    assert results[0]["ok"] is True, results[0]
    tool_msgs = _tool_messages(llm, base)
    assert "in:来自 stdin 的内容" in tool_msgs[0]["content"], tool_msgs[0]

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done"
