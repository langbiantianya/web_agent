"""端到端：配置 Agent/Workflow → 建会话 → 发消息 → SSE 吐字 → 落库 → 续传。

用一个真实的 OpenAI 兼容 HTTP 服务当 LLM 后端（tests/fake_llm.py），
因此 AsyncOpenAI 的真实网络栈、流式解析、重连等行为都被覆盖。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.config.db import SessionLocal
from app.core.run_hub import get_hub
from app.main import create_app
from app.repo.run_event_repo import run_event_repo
from app.repo.run_repo import run_repo
from tests.fake_llm import FakeLLM


@pytest.fixture(scope="module")
def llm() -> FakeLLM:
    """整个模块共用一个假 LLM 服务。

    scope=module：每个用例都重启一次 HTTP 服务太慢，而假服务本身无状态
    （按调用序返回 texts），只有 calls 记录会跨用例累积。
    """
    server = FakeLLM(texts=["规划", "完成", "答案"], chunk_delay=0.0)
    server.start()
    yield server
    server.stop()


@pytest.fixture
async def client(llm: FakeLLM):
    """每个用例一份全新的 app + HTTP 客户端。

    必须手动走 lifespan：RunHub、事件写手这些后台组件都在 startup 里注册，
    跳过它 SSE 直接没有订阅者。
    """
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            yield c


# ------------------------------------------------------------------ helpers


async def _make_agent(client: httpx.AsyncClient, code: str, llm: FakeLLM):
    """建一个指向假 LLM 的 Agent，返回服务端返回的配置体。"""
    r = await client.post(
        "/api/agents",
        json={
            "code": code,
            "name": f"Agent {code}",
            "system_prompt": f"你是 {code}",
            "model": "fake-model",
            "base_url": llm.base_url,
            "api_key": "test-key",
            "temperature": 0.5,
            "max_tokens": 256,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _make_workflow(client: httpx.AsyncClient, codes: list[str], llm: FakeLLM):
    """按给定顺序串一条线性流水线：相邻两个 code 之间连一条边。"""
    nodes = [
        {"id": c, "agentCode": c, "outputKey": f"{c}_out"} for c in codes
    ]
    edges = [{"from": a, "to": b} for a, b in zip(codes, codes[1:])]
    r = await client.post(
        "/api/workflows",
        json={
            "code": f"wf_{'_'.join(codes)}",
            "name": "测试流水线",
            "definition": {
                "entry": codes[0],
                "nodes": nodes,
                "edges": edges,
                "inputTemplate": "问题：{{user_input}}\n上游：{{upstream}}",
            },
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _new_conversation(client: httpx.AsyncClient, wf_code: str):
    """开一个新会话；对话必须挂在一个 workflow 上，否则没有编排器可跑。"""
    r = await client.post(
        "/api/conversations", json={"workflow_code": wf_code, "user_id": "u1"}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _ticket(client: httpx.AsyncClient) -> str:
    """取一张一次性 SSE 凭证：EventSource 不能带自定义头，只能走 query。"""
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
                data = json.loads(line[5:].strip())
                out.append((event, eid, data))
                if event in ("done", "error"):
                    break
    return out


async def _wait_run_status(run_id: int, statuses: set[str], timeout: float = 10.0):
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


async def _wait_event_count(run_id: int, minimum: int, timeout: float = 10.0) -> int:
    """轮询等待已落库事件数达到 minimum。"""
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        async with SessionLocal() as s:
            seen = await run_event_repo.count_after(s, run_id, 0)
        if seen >= minimum:
            return seen
        await asyncio.sleep(0.02)
    return seen


# -------------------------------------------------------------------- 测试


async def test_health(client: httpx.AsyncClient):
    """健康检查必须真的探一下 DB，而不是固定回 ok。

    只回 ok 的健康检查会让「连不上库」的服务继续被负载均衡打流量。
    """
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


async def test_agent_crud_and_version_bump(client: httpx.AsyncClient, llm: FakeLLM):
    """增删改查 + 三条约束：code 唯一、api_key 不回显、改配置版本号自增。

    版本号是「配置有没有变过」的判据，不自增就没法让编排器知道要换 Prompt。
    """
    created = await _make_agent(client, "solo_agent", llm)
    assert created["code"] == "solo_agent"
    assert created["has_api_key"] is True
    assert "api_key" not in created  # 明文 key 绝不出现在响应里

    dup = await client.post(
        "/api/agents",
        json={
            "code": "solo_agent",
            "name": "dup",
            "system_prompt": "x",
            "model": "m",
            "base_url": llm.base_url,
        },
    )
    assert dup.status_code == 409

    updated_resp = await client.put(
        "/api/agents/solo_agent", json={"temperature": 0.9}
    )
    assert updated_resp.status_code == 200
    updated = updated_resp.json()
    assert updated["version"] == created["version"] + 1

    assert (await client.get("/api/agents/solo_agent")).status_code == 200
    assert (await client.get("/api/agents/nope")).status_code == 404


async def test_workflow_update_with_definition_is_not_500(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """回归：PUT 带 definition 时不能 500。

    这里曾经有个必现的 500：``payload.model_dump(exclude_unset=True)`` 会把嵌套的
    ``definition`` 递归转成普通 dict，紧接着代码又对这个 dict 调
    ``.model_dump()`` → AttributeError。整个接口只要带 definition 提交就挂，
    而老测试只覆盖了 POST 建 Workflow，一直没人发现。

    断言两条：状态码 200；回读的定义里节点是改过的那批（确认真的写进去了，
    而不是「没报错但也没保存」）。
    """
    created = await _make_workflow(client, ["upd_a1", "upd_a2"], llm)
    code = created["code"]

    r = await client.put(
        f"/api/workflows/{code}",
        json={
            "definition": {
                "entry": "upd_a1",
                "nodes": [
                    {"id": "upd_a1", "agentCode": "upd_a1", "outputKey": "p"},
                    {"id": "upd_a2", "agentCode": "upd_a2", "outputKey": "f"},
                ],
                "edges": [{"from": "upd_a1", "to": "upd_a2"}],
                "inputTemplate": "{{user_input}}",
            }
        },
    )

    assert r.status_code == 200, r.text
    assert [n["id"] for n in r.json()["definition"]["nodes"]] == ["upd_a1", "upd_a2"]

    # 再回读一次，确认真的落库了
    back = await client.get(f"/api/workflows/{code}")
    assert back.status_code == 200
    assert back.json()["definition"]["entry"] == "upd_a1"


async def test_workflow_update_partial_keeps_other_fields(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """局部更新：只传 enabled 时，name 与 definition 必须原样保留。"""
    created = await _make_workflow(client, ["part_a1", "part_a2"], llm)
    code = created["code"]
    original = created["definition"]

    r = await client.put(f"/api/workflows/{code}", json={"enabled": False})

    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["name"] == created["name"]
    assert body["definition"]["nodes"] == original["nodes"]


async def test_workflow_accepts_dag_definition(client: httpx.AsyncClient):
    """V3 起 API 层接受分叉图：a 分出 b/c，b 再汇入 c。"""
    r = await client.post(
        "/api/workflows",
        json={
            "code": "ok_dag",
            "name": "dag",
            "definition": {
                "nodes": [
                    {"id": "a", "agentCode": "a"},
                    {"id": "b", "agentCode": "b"},
                    {"id": "c", "agentCode": "c"},
                ],
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "a", "to": "c"},
                    {"from": "b", "to": "c"},
                ],
            },
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1


async def test_workflow_rejects_orphan_node(client: httpx.AsyncClient):
    """孤立节点（从 entry 走不到）仍必须在 API 层被拒。

    这条曾经是「DAG 被拒」，V3 放开 DAG 后换成了真正该拒的图——配置期挡不住的
    错误只会推迟到编排中途才炸，那时 token 和钱已经花出去了。
    """
    r = await client.post(
        "/api/workflows",
        json={
            "code": "bad_orphan",
            "name": "orphan",
            "definition": {
                "entry": "a",
                "nodes": [
                    {"id": "a", "agentCode": "a"},
                    {"id": "lost", "agentCode": "lost"},
                ],
                "edges": [],
            },
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "bad_request"
    # 顶层 message 是统一的「请求参数校验失败」，具体原因在 data.errors 里——
    # 后台页把那条 msg 直接展示给用户，所以它必须自带可读信息。
    detail = body["data"]["errors"][0]["msg"]
    assert "孤立节点" in detail, detail


async def test_full_pipeline_streams_and_persists(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """三 Agent 线性流水线跑通：SSE 吐字 + 事件落库 + 消息落库。"""
    for code in ("alpha", "beta"):
        await _make_agent(client, code, llm)
    wf = await _make_workflow(client, ["alpha", "beta"], llm)
    conv = await _new_conversation(client, wf["code"])

    sent = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "介绍一下 SSE"}
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run_id"]

    ticket = await _ticket(client)
    events = await _read_sse(
        client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0"
    )

    kinds = [e for e, _, _ in events]
    assert kinds[0] == "node_start"
    assert kinds[-1] == "done"
    assert kinds.count("node_start") == 2
    assert kinds.count("node_end") == 2
    assert "token" in kinds

    # seq 严格递增
    ids = [i for _, i, _ in events]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))

    # 每个节点都吐了字
    tokens = "".join(d["content"] for e, _, d in events if e == "token")
    assert tokens

    # run 落库为 done（done 事件先于状态回写，轮询等它收敛）
    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None and run.status == "done", "run 未正常结束"
    assert run.last_seq == ids[-1]
    async with SessionLocal() as db:
        steps = await run_repo.list_steps(db, run_id)
        assert [s.status for s in steps] == ["done", "done"]

    # 每个节点的产出都成了 assistant 消息
    msgs = (await client.get(f"/api/conversations/{conv['id']}/messages")).json()
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "assistant"]


async def test_concurrent_message_on_same_conversation_returns_409(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """一个会话同时只允许一个活跃 run，否则两次生成会互相串流。

    并发窗口靠「先等 run 真的进 running 再发第二条」构造，不用 sleep 猜。
    """
    slow = FakeLLM(texts=["慢速回复"], chunk_delay=0.05)
    slow.start()
    try:
        await _make_agent(client, "slow_agent", slow)
        wf = await _make_workflow(client, ["slow_agent"], slow)
        conv = await _new_conversation(client, wf["code"])

        first = await client.post(
            f"/api/conversations/{conv['id']}/messages", json={"content": "问题1"}
        )
        assert first.status_code == 202
        run_id = first.json()["run_id"]

        # 等后台任务把 run 推进到 running，再试第二次
        assert await _wait_run_status(run_id, {"running", "done"}) is not None

        second = await client.post(
            f"/api/conversations/{conv['id']}/messages", json={"content": "问题2"}
        )
        assert second.status_code == 409
        assert second.json()["code"] == "conflict"

        # 等第一个跑完，别留后台任务
        await _wait_run_status(run_id, {"done", "error", "cancelled"})
    finally:
        slow.stop()


async def test_active_run_reflects_running_state(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """active-run 要能在 run 进行中指出来、结束后立刻变空。

    收尾后仍报着一个 run，前端会一直显示「生成中」且发不出新消息。
    """
    slow = FakeLLM(texts=["一", "二", "三", "四"], chunk_delay=0.08)
    slow.start()
    try:
        await _make_agent(client, "active_agent", slow)
        wf = await _make_workflow(client, ["active_agent"], slow)
        conv = await _new_conversation(client, wf["code"])
        sent = await client.post(
            f"/api/conversations/{conv['id']}/messages", json={"content": "问题"}
        )
        run_id = sent.json()["run_id"]

        active = await client.get(f"/api/conversations/{conv['id']}/active-run")
        assert active.status_code == 200
        assert active.json()["run_id"] == run_id

        await _wait_run_status(run_id, {"done", "error"})

        after = await client.get(f"/api/conversations/{conv['id']}/active-run")
        assert after.json() is None
    finally:
        slow.stop()


async def test_resume_after_disconnect_replays_remaining_tokens(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """核心场景：读到一半断开，用 last_seq 重连，必须把后续 token 补齐。"""
    full_text = "第一段内容第二段内容第三段内容"
    # 单节点 → 只调用模型一次，texts[0] 就是完整输出
    slow = FakeLLM(texts=[full_text], chunk_delay=0.05)
    slow.start()
    try:
        await _make_agent(client, "resume_agent", slow)
        wf = await _make_workflow(client, ["resume_agent"], slow)
        conv = await _new_conversation(client, wf["code"])
        sent = await client.post(
            f"/api/conversations/{conv['id']}/messages", json={"content": "问题"}
        )
        run_id = sent.json()["run_id"]
        ticket = await _ticket(client)

        # --- 第一次连接：读到 2 个 token 就主动断开 ---
        first_batch: list[tuple[str, int, dict]] = []
        async with client.stream(
            "GET", f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0"
        ) as resp:
            assert resp.status_code == 200
            event, eid = "message", 0
            async for line in resp.aiter_lines():
                line = line.rstrip("\r")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("id:"):
                    eid = int(line[3:].strip())
                elif line.startswith("data:"):
                    first_batch.append((event, eid, json.loads(line[5:].strip())))
                    if event == "token" and len(
                        [1 for e, _, _ in first_batch if e == "token"]
                    ) >= 2:
                        break

        assert len(first_batch) >= 3
        last_seq = first_batch[-1][1]

        # --- 第二次连接：带上 last_seq，服务端回放剩余 ---
        resumed = await _read_sse(
            client, f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq={last_seq}"
        )

        assert all(i > last_seq for _, i, _ in resumed), "重连后重复发了已收到的 seq"
        assert resumed[-1][0] == "done"

        # 拼起来 == 模型完整输出，不漏字也不重复
        original = "".join(d["content"] for e, _, d in first_batch if e == "token")
        rest = "".join(d["content"] for e, _, d in resumed if e == "token")
        assert original and rest, "断点两侧都应该有内容"
        assert original + rest == full_text
    finally:
        slow.stop()


async def test_last_event_id_header_takes_priority(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """浏览器内置重连带的是 Last-Event-ID 头，必须优先于 last_seq。"""
    await _make_agent(client, "header_agent", llm)
    wf = await _make_workflow(client, ["header_agent"], llm)
    conv = await _new_conversation(client, wf["code"])
    sent = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "问题"}
    )
    run_id = sent.json()["run_id"]
    ticket = await _ticket(client)

    await _read_sse(client, f"/api/runs/{run_id}/stream?ticket={ticket}")

    # 头里说从 seq=2 开始，query 说 0 —— 以头为准
    async with client.stream(
        "GET",
        f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0",
        headers={"Last-Event-ID": "2"},
    ) as resp:
        assert resp.status_code == 200
        first_id = None
        event = ""
        async for line in resp.aiter_lines():
            if line.startswith("id:"):
                first_id = int(line[3:].strip())
                break
    assert first_id is not None and first_id > 2


async def test_stream_requires_valid_ticket(client: httpx.AsyncClient, llm: FakeLLM):
    """SSE 不能凭 run_id 裸订阅——凭证是防「猜 id 就能看别人输出」的唯一闸门。"""
    r = await client.get("/api/runs/1/stream?ticket=garbage")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


async def test_stream_on_missing_run_is_404(client: httpx.AsyncClient):
    """run 不存在要 404 而不是「开一条空流等消息」——那会让前端永远转圈。"""
    ticket = await _ticket(client)
    r = await client.get(f"/api/runs/999999/stream?ticket={ticket}")
    assert r.status_code == 404


async def test_cancel_stops_generation(client: httpx.AsyncClient):
    """取消必须真正掐断上游模型流：run 收敛到 cancelled 且 hub 里不再活跃。

    只改 DB 状态、后台任务还在跑的话，token 会继续写库并被计费。
    """
    # 跑道要够宽：假模型按 2 字一片切，60 字 → 30 片 × 0.05s ≈ 1.5s 的输出。
    # 用例只在开头等 3 个事件就取消，留出 ~1.4s 余量——机器有负载时（并行跑测试、
    # 别的进程抢 CPU）窗口太窄会导致「取消还没到，run 已经跑完」而偶发失败。
    slow = FakeLLM(texts=["甲乙丙丁戊己庚辛壬癸" * 6], chunk_delay=0.05)
    slow.start()
    try:
        await _make_agent(client, "cancel_agent", slow)
        wf = await _make_workflow(client, ["cancel_agent"], slow)
        conv = await _new_conversation(client, wf["code"])
        sent = await client.post(
            f"/api/conversations/{conv['id']}/messages", json={"content": "长任务"}
        )
        run_id = sent.json()["run_id"]

        # 等真的吐了几个字再取消。
        # 注意：run.last_seq 是 run 结束时才统一回写的（避免写放大），
        # 进行中必须去看 run_event 才能知道吐到哪了。
        assert await _wait_event_count(run_id, 3) >= 3

        cancelled = await client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["cancelled"] is True, (
            f"取消未被受理：{cancelled.text}"
        )

        run = await _wait_run_status(run_id, {"cancelled", "done", "error"})
        assert run is not None
        assert run.status == "cancelled", (
            f"期望 cancelled，实际 {run.status}（若为 done 说明取消慢了一步）"
        )
        assert not get_hub().is_active(run_id)
    finally:
        slow.stop()


async def test_node_failure_retries_then_marks_error(
    client: httpx.AsyncClient, llm: FakeLLM
):
    """Agent 指向一个不存在的端口：重试 2 次后 run 变 error。"""
    dead = FakeLLM()  # 只为拿一个「没人监听」的端口
    await _make_agent(client, "dead_agent", dead)
    wf = await _make_workflow(client, ["dead_agent"], llm)
    conv = await _new_conversation(client, wf["code"])
    sent = await client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": "会失败"}
    )
    run_id = sent.json()["run_id"]

    run = await _wait_run_status(run_id, {"error", "done"}, timeout=20.0)
    assert run is not None
    assert run.status == "error"
    assert run.error_msg
    async with SessionLocal() as db:
        steps = await run_repo.list_steps(db, run_id)
        assert steps and steps[-1].status == "error"
