"""Agent JSON 响应值配置（输出格式 = json + 可选 JSON Schema）。"""

from __future__ import annotations

import asyncio
import itertools
import time

import httpx
import pytest

from app.core.agent_response_format import (
    build_response_format_messages,
    is_json_response,
    json_instruction,
    normalize_response_format,
    parse_json_response,
    parse_response_format,
    parse_response_schema,
    strip_code_fence,
)
from app.config.db import SessionLocal
from app.main import create_app
from app.repo.run_repo import run_repo
from tests.conftest import authenticate_admin, authenticate_client
from tests.fake_llm import FakeLLM


# ------------------------------------------------------------------ 核心模块


def test_normalize_defaults_to_text():
    """空值与未知值都按文本处理——读路径不该因为脏值炸掉。"""
    assert normalize_response_format(None) == "text"
    assert normalize_response_format("") == "text"
    assert normalize_response_format("xml") == "text"


def test_normalize_is_case_insensitive():
    """大小写与空白不敏感。"""
    assert normalize_response_format(" JSON ") == "json"
    assert normalize_response_format("JSON") == "json"


def test_parse_response_format_rejects_unknown():
    """写路径上非法值必须报错，不能静默回落成 text。"""
    with pytest.raises(ValueError):
        parse_response_format("xml")


def test_parse_response_format_empty_is_text():
    """空值等价于不配置。"""
    assert parse_response_format("") == "text"
    assert parse_response_format(None) == "text"


def test_is_json_response():
    """只有 json 才算 JSON 模式。"""
    assert is_json_response("json") is True
    assert is_json_response("text") is False
    assert is_json_response(None) is False


def test_json_instruction_without_schema():
    """没有 schema 时只要求「顶层是 JSON 对象」。"""
    text = json_instruction(None)
    assert "JSON" in text
    assert "JSON Schema" not in text


def test_json_instruction_with_schema():
    """给了 schema 就把结构要求写进指令里。"""
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    text = json_instruction(schema)
    assert "JSON Schema" in text
    assert '"answer"' in text


def test_build_messages_text_mode_is_empty():
    """文本模式一条消息都不加——提示词不能因为无关配置而变长。"""
    assert build_response_format_messages("text", {"type": "object"}) == []


def test_build_messages_json_mode_adds_one_system_message():
    """JSON 模式追加且只追加一条 system 消息。"""
    msgs = build_response_format_messages("json", None)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"
    assert "JSON" in msgs[0]["content"]


def test_strip_code_fence():
    """代码围栏整段剥掉，没有围栏时原样返回。"""
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_parse_json_response_plain():
    """纯 JSON 直接解析。"""
    value, reason = parse_json_response('{"a": 1}')
    assert value == {"a": 1}
    assert reason == ""


def test_parse_json_response_code_fence():
    """模型把 JSON 包在围栏里也要能取出来。"""
    value, reason = parse_json_response('```json\n{"a": 1}\n```')
    assert value == {"a": 1}
    assert reason == ""


def test_parse_json_response_with_prose_prefix():
    """前面带一句客套话，但 JSON 在围栏里——照样认。"""
    value, reason = parse_json_response('好的，结果如下：\n```json\n{"a": 1}\n```')
    assert value == {"a": 1}
    assert reason == ""


def test_parse_json_response_invalid():
    """解析不了就给出原因，而不是抛异常。"""
    value, reason = parse_json_response("这不是 JSON")
    assert value is None
    assert reason


def test_parse_json_response_empty():
    """空输出有专门的说法。"""
    value, reason = parse_json_response("")
    assert value is None
    assert "没有返回" in reason


def test_parse_response_schema_variants():
    """Schema 文本：空表示不限定，非法 JSON / 非对象都要拒。"""
    assert parse_response_schema(None) is None
    assert parse_response_schema("") is None
    assert parse_response_schema('{"type": "object"}') == {"type": "object"}
    with pytest.raises(ValueError):
        parse_response_schema("{oops")
    with pytest.raises(ValueError):
        parse_response_schema("[1, 2]")


# -------------------------------------------------------------------- API 层


_counter = itertools.count()


@pytest.fixture
async def client():
    """真实 app + HTTP 客户端。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            await authenticate_admin(c)
            yield c


def _agent_payload(code: str, **extra) -> dict:
    """一份能建出 Agent 的最小请求体。"""
    payload = {
        "code": code,
        "name": code,
        "system_prompt": "你是助手",
        "model": "m",
        "base_url": "http://x",
    }
    payload.update(extra)
    return payload


async def test_create_agent_with_json_format(client):
    """建一个 JSON 模式的 Agent，两个字段都要回显。"""
    code = f"json_agent_{next(_counter)}"
    r = await client.post(
        "/api/agents",
        json=_agent_payload(
            code,
            response_format="json",
            response_schema={"type": "object"},
        ),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["response_format"] == "json"
    assert body["response_schema"] == {"type": "object"}


async def test_create_agent_default_is_text(client):
    """不配置时默认文本模式。"""
    code = f"plain_agent_{next(_counter)}"
    r = await client.post("/api/agents", json=_agent_payload(code))
    assert r.status_code == 201, r.text
    assert r.json()["response_format"] == "text"
    assert r.json()["response_schema"] is None


async def test_create_agent_rejects_unknown_response_format(client):
    """非法输出格式在接口层就被拒（400 + 字段级错误）。"""
    code = f"bad_agent_{next(_counter)}"
    r = await client.post(
        "/api/agents", json=_agent_payload(code, response_format="xml")
    )
    assert r.status_code == 400, r.text


async def test_create_agent_rejects_non_object_schema(client):
    """schema 必须是对象，数组不行。"""
    code = f"bad_schema_{next(_counter)}"
    r = await client.post(
        "/api/agents", json=_agent_payload(code, response_schema=[1, 2])
    )
    assert r.status_code == 400, r.text


async def test_update_agent_clears_response_schema(client):
    """显式传 null 表示「清空结构要求」，而不是「不修改」。"""
    code = f"clear_agent_{next(_counter)}"
    created = await client.post(
        "/api/agents",
        json=_agent_payload(
            code, response_format="json", response_schema={"type": "object"}
        ),
    )
    assert created.status_code == 201, created.text

    r = await client.put(f"/api/agents/{code}", json={"response_schema": None})
    assert r.status_code == 200, r.text
    assert r.json()["response_schema"] is None
    # response_format 不受影响
    assert r.json()["response_format"] == "json"


async def test_update_agent_keeps_schema_when_absent(client):
    """不传该字段时保持原样。"""
    code = f"keep_agent_{next(_counter)}"
    created = await client.post(
        "/api/agents",
        json=_agent_payload(
            code, response_format="json", response_schema={"type": "object"}
        ),
    )
    assert created.status_code == 201, created.text

    r = await client.put(f"/api/agents/{code}", json={"description": "改个描述"})
    assert r.status_code == 200, r.text
    assert r.json()["response_schema"] == {"type": "object"}


async def test_prompt_preview_matches_runtime_instruction(client):
    """预览里的 JSON 指令必须与运行时发出的**逐字一致**。"""
    code = f"preview_agent_{next(_counter)}"
    created = await client.post(
        "/api/agents",
        json=_agent_payload(
            code,
            response_format="json",
            response_schema={"type": "object", "required": ["answer"]},
        ),
    )
    assert created.status_code == 201, created.text

    r = await client.get(f"/api/agents/{code}/prompt-preview")
    assert r.status_code == 200, r.text
    messages = r.json()["messages"]
    expected = build_response_format_messages("json", {"type": "object", "required": ["answer"]})
    assert messages[-1] == expected[0]


async def test_prompt_preview_text_agent_has_no_json_instruction(client):
    """文本模式的预览里不应出现输出格式指令。"""
    code = f"plain_preview_{next(_counter)}"
    created = await client.post("/api/agents", json=_agent_payload(code))
    assert created.status_code == 201, created.text

    r = await client.get(f"/api/agents/{code}/prompt-preview")
    assert r.status_code == 200, r.text
    contents = [m.get("content", "") for m in r.json()["messages"]]
    assert not any("【输出格式】" in c for c in contents)


# ------------------------------------------------------------------ 运行期


@pytest.fixture
async def llm_client():
    """带假 LLM 的客户端；每个用例一份全新的假服务。"""
    llm = FakeLLM(texts=["ok"])
    llm.start()
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            await authenticate_admin(c)
            await authenticate_client(c)
            yield c, llm
    llm.stop()


async def _make_json_agent(client: httpx.AsyncClient, code: str, llm: FakeLLM):
    """建一个 JSON 模式的 Agent（指向假 LLM）。"""
    r = await client.post(
        "/api/agents",
        json={
            "code": code,
            "name": code,
            "system_prompt": "你是助手",
            "model": "fake-model",
            "base_url": llm.base_url,
            "api_key": "k",
            "response_format": "json",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _wait_run_status(run_id: int, statuses: set[str], timeout: float = 30.0):
    """轮询等待 run 进入指定状态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with SessionLocal() as s:
            run = await run_repo.get_by_id(s, run_id)
            if run is not None and run.status in statuses:
                return run
        await asyncio.sleep(0.02)
    return None


async def _run_once(client: httpx.AsyncClient, code: str, content: str = "hi") -> int:
    """建流水线 → 开会话 → 发消息，返回 run_id。"""
    wf = await client.post(
        "/api/workflows",
        json={
            "code": f"wf_{code}",
            "name": "流水线",
            "definition": {
                "entry": code,
                "nodes": [{"id": code, "agentCode": code, "outputKey": "out"}],
                "edges": [],
                "inputTemplate": "{{user_input}}",
            },
        },
    )
    assert wf.status_code == 201, wf.text
    conv = await client.post("/api/conversations", json={"workflow_code": f"wf_{code}"})
    assert conv.status_code == 201, conv.text
    sent = await client.post(
        f"/api/conversations/{conv.json()['id']}/messages", json={"content": content}
    )
    assert sent.status_code in (200, 201, 202), sent.text
    return sent.json()["run_id"]


async def test_json_agent_returns_parsed_json(llm_client):
    """模型返回 JSON 时，节点输出里带上解析后的结果。"""
    client, llm = llm_client
    llm.texts = ['{"answer": "42"}']
    code = f"json_ok_{next(_counter)}"
    await _make_json_agent(client, code, llm)
    run_id = await _run_once(client, code)

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None
    assert run.status == "done", run.error_msg

    async with SessionLocal() as db:
        steps = await run_repo.list_steps(db, run_id)
    assert steps
    output = steps[-1].output_json or {}
    assert output.get("responseFormat") == "json"
    assert output.get("json") == {"answer": "42"}


async def test_json_agent_rejects_prose_output(llm_client):
    """模型返回散文时，节点重试后仍以「没有合法 JSON」失败。"""
    client, llm = llm_client
    llm.texts = ["好的，这就是答案"]
    code = f"json_bad_{next(_counter)}"
    await _make_json_agent(client, code, llm)
    run_id = await _run_once(client, code)

    run = await _wait_run_status(run_id, {"done", "error"})
    assert run is not None
    assert run.status == "error"
    assert "JSON" in (run.error_msg or "")


# ------------------------------------------------------------------ 管理端


async def test_admin_form_values_reject_bad_response_format():
    """管理端表单的非法输出格式报字段级错误。"""
    from services.admin_web.agent_forms import _business_field_errors

    errors = _business_field_errors(ValueError("invalid_response_format"))
    assert errors["response_format"]


async def test_admin_form_values_reject_bad_response_schema():
    """管理端表单的非法 schema 报字段级错误。"""
    from services.admin_web.agent_forms import _business_field_errors

    errors = _business_field_errors(ValueError("invalid_response_schema"))
    assert errors["response_schema"]
