"""数据库 LLM 接口配置、密钥边界与故障切换测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError

from app.config.settings import settings
from app.core.agent_runner import AgentRunner
from app.core.crypto import decrypt, encrypt
from app.core.llm_endpoints import LlmEndpointSpec, load_llm_endpoints
from app.core.run_hub import RunHub
from app.main import create_app
from app.models.agent import Agent
from app.models.llm_endpoint import LlmEndpoint
from app.repo.llm_endpoint_repo import llm_endpoint_repo


class _FakeStream:
    """可按给定片段返回内容，或在中途抛错的异步流。"""

    def __init__(self, chunks, error: Exception | None = None) -> None:
        """保存待返回片段与可选的中途错误。"""
        self._chunks = chunks
        self._error = error

    def __aiter__(self):
        """返回异步迭代器自身。"""
        self._iterator = self._iterate()
        return self

    async def __anext__(self):
        """迭代并传播预设错误。"""
        return await self._iterator.__anext__()

    async def _iterate(self):
        """逐片产出 OpenAI stream chunk 形状。"""
        for text in self._chunks:
            yield SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=text,
                            reasoning_content=None,
                            tool_calls=None,
                        )
                    )
                ],
            )
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        """模拟关闭流。"""


class _FakeClient:
    """返回固定流或抛错的 AsyncOpenAI 替身。"""

    def __init__(
        self,
        chunks=None,
        error: Exception | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        """构造 chat.completions.create 的替身。"""
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )
        self._chunks = chunks or []
        self._error = error
        self._stream_error = stream_error

    async def _create(self, **_kwargs):
        """首次创建请求时抛错，否则返回预设流。"""
        if self._error is not None:
            raise self._error
        return _FakeStream(self._chunks, self._stream_error)


def _connection_error() -> APIConnectionError:
    """构造不包含真实凭据的连接失败异常。"""
    return APIConnectionError(request=httpx.Request("POST", "http://llm.test/v1"))


def _agent() -> Agent:
    """构造仅在内存中使用的兼容 Agent。"""
    return Agent(
        code="fallback-test",
        name="fallback-test",
        system_prompt="你是测试助手",
        model="fake-model",
        base_url="http://legacy.test/v1",
        api_key_cipher="x",
    )


@pytest.mark.asyncio
async def test_endpoint_fallback_before_output(monkeypatch):
    """首个接口在吐字前失败时，应自动切换到下一优先级。"""
    endpoints = (
        LlmEndpointSpec("primary", "主接口", "http://primary/v1", "a", 10),
        LlmEndpointSpec("backup", "备用接口", "http://backup/v1", "b", 20),
    )
    calls: list[str] = []
    clients = {
        "primary": _FakeClient(error=_connection_error()),
        "backup": _FakeClient(chunks=["fallback-ok"]),
    }

    def _get(endpoint: LlmEndpointSpec):
        """记录尝试顺序并返回对应替身。"""
        calls.append(endpoint.code)
        return clients[endpoint.code]

    monkeypatch.setattr("app.core.agent_runner._client_factory.get", _get)
    runner = AgentRunner(
        agent=_agent(),
        node_id="n1",
        run_id=1,
        hub=RunHub(),
        user_texts=[],
        endpoints=endpoints,
    )

    result = await runner.run([{"role": "user", "content": "hello"}])

    assert calls == ["primary", "backup"]
    assert result.text == "fallback-ok"
    assert runner._endpoint_index == 1


@pytest.mark.asyncio
async def test_no_fallback_after_partial_output(monkeypatch):
    """已经向客户端吐字后不能再切换，否则会重复输出。"""
    endpoints = (
        LlmEndpointSpec("primary", "主接口", "http://primary/v1", "a", 10),
        LlmEndpointSpec("backup", "备用接口", "http://backup/v1", "b", 20),
    )
    calls: list[str] = []
    clients = {
        "primary": _FakeClient(chunks=["partial"], stream_error=_connection_error()),
        "backup": _FakeClient(chunks=["must-not-run"]),
    }

    def _get(endpoint: LlmEndpointSpec):
        """记录调用，第二个端点不应被访问。"""
        calls.append(endpoint.code)
        return clients[endpoint.code]

    monkeypatch.setattr("app.core.agent_runner._client_factory.get", _get)
    runner = AgentRunner(
        agent=_agent(),
        node_id="n1",
        run_id=2,
        hub=RunHub(),
        user_texts=[],
        endpoints=endpoints,
    )

    with pytest.raises(APIConnectionError):
        await runner.run([{"role": "user", "content": "hello"}])

    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_load_endpoints_uses_priority_and_skips_disabled(db):
    """运行期只加载启用项，并按 priority 从小到大排序。"""
    suffix = uuid4().hex[:8]
    codes = [f"p30-{suffix}", f"p10-{suffix}", f"p20-{suffix}"]
    priorities = (30, 10, 20)
    created: list[str] = []
    try:
        for code, priority in zip(codes, priorities, strict=True):
            await llm_endpoint_repo.create(
                db,
                code=code,
                name=code,
                base_url=f"https://{code}.test/v1",
                api_key_cipher="cipher",
                priority=priority,
                enabled=1,
            )
            created.append(code)
        disabled = f"disabled-{suffix}"
        await llm_endpoint_repo.create(
            db,
            code=disabled,
            name=disabled,
            base_url=f"https://{disabled}.test/v1",
            api_key_cipher="cipher",
            priority=0,
            enabled=0,
        )
        created.append(disabled)

        specs = await load_llm_endpoints(_agent())
        subset = [spec.code for spec in specs if spec.code in set(codes)]

        assert subset == [f"p10-{suffix}", f"p20-{suffix}", f"p30-{suffix}"]
        assert disabled not in {spec.code for spec in specs}
    finally:
        for code in created:
            await llm_endpoint_repo.delete(db, code)


@pytest.mark.asyncio
async def test_all_disabled_configs_do_not_fall_back_to_legacy(monkeypatch):
    """数据库已有配置但全部停用时，旧 Agent 地址也不能重新生效。"""
    async def _no_enabled(*_args, **_kwargs):
        """模拟没有启用项。"""
        return []

    async def _has_config(*_args, **_kwargs):
        """模拟数据库仍有停用配置。"""
        return True

    monkeypatch.setattr(llm_endpoint_repo, "list", _no_enabled)
    monkeypatch.setattr(llm_endpoint_repo, "has_any", _has_config)

    assert await load_llm_endpoints(_agent()) == ()


def test_endpoint_api_crud_masks_key_and_orders_results():
    """管理接口加密保存 Key、永不回显，并按优先级排序。"""
    client = TestClient(create_app())
    headers = {"X-Admin-Key": settings.admin_key}
    suffix = uuid4().hex[:8]
    low = f"low-{suffix}"
    high = f"high-{suffix}"

    first = client.post(
        "/api/llm-endpoints",
        headers=headers,
        json={
            "code": low,
            "name": "低优先级",
            "base_url": "https://low.example/v1/",
            "api_key": "super-secret",
            "priority": 50,
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["has_api_key"] is True
    assert "api_key" not in first.json()
    assert "cipher" not in first.text
    assert first.json()["base_url"] == "https://low.example/v1"

    second = client.post(
        "/api/llm-endpoints",
        headers=headers,
        json={
            "code": high,
            "name": "高优先级",
            "base_url": "https://high.example/v1",
            "api_key": "other-secret",
            "priority": 5,
        },
    )
    assert second.status_code == 201, second.text

    listed = client.get("/api/llm-endpoints", headers=headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert [(row["priority"], row["id"]) for row in rows] == sorted(
        (row["priority"], row["id"]) for row in rows
    )
    positions = {row["code"]: i for i, row in enumerate(rows)}
    assert positions[high] < positions[low]

    updated = client.put(
        f"/api/llm-endpoints/{low}",
        headers=headers,
        json={"api_key": "", "enabled": False},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["has_api_key"] is False
    assert updated.json()["enabled"] is False
    assert updated.json()["version"] == first.json()["version"] + 1

    enabled_only = client.get(
        "/api/llm-endpoints?only_enabled=true", headers=headers
    )
    assert enabled_only.status_code == 200, enabled_only.text
    enabled_codes = {row["code"] for row in enabled_only.json()}
    assert low not in enabled_codes
    assert high in enabled_codes

    assert client.get("/api/llm-endpoints").status_code == 401
    assert client.delete(
        f"/api/llm-endpoints/{low}", headers=headers
    ).status_code == 204
    assert client.delete(
        f"/api/llm-endpoints/{high}", headers=headers
    ).status_code == 204


def test_agent_can_use_database_endpoint_without_legacy_fields():
    """新 Agent 不必再复制地址与 Key，接口由全局配置提供。"""
    client = TestClient(create_app())
    code = f"db-agent-{uuid4().hex[:8]}"
    created = client.post(
        "/api/agents",
        json={
            "code": code,
            "name": "Database Agent",
            "system_prompt": "你是助手",
            "model": "fake-model",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["base_url"] == ""
    assert created.json()["has_api_key"] is False
    assert client.delete(f"/api/agents/{code}").status_code == 204


def test_admin_page_exposes_llm_endpoint_management():
    """后台必须能发现数据库接口管理入口，而不是继续只编辑 Agent 单端点。"""
    client = TestClient(create_app())
    page = client.get("/admin")

    assert page.status_code == 200
    assert "LLM 接口" in page.text
    assert "/api/llm-endpoints" in page.text
    assert 'id="a_base"' not in page.text
    assert 'id="a_key"' not in page.text


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://llm.example/v1",
        "https://user:password@llm.example/v1",
        "not-a-url",
    ],
)
def test_endpoint_api_rejects_unsafe_or_invalid_url(base_url):
    """接口地址只接受不内嵌凭据的 HTTP(S) URL。"""
    client = TestClient(create_app())
    response = client.post(
        "/api/llm-endpoints",
        headers={"X-Admin-Key": settings.admin_key},
        json={
            "code": f"bad-{uuid4().hex[:8]}",
            "name": "bad",
            "base_url": base_url,
            "api_key": "secret",
        },
    )

    assert response.status_code == 400
    assert "password" not in response.text


@pytest.mark.asyncio
async def test_repo_encrypts_key_before_storage(db):
    """写入数据库的是密文，只有运行期解密才能得到原始 Key。"""
    code = f"enc-{uuid4().hex[:8]}"
    cipher = encrypt("plain-secret")
    try:
        await llm_endpoint_repo.create(
            db,
            code=code,
            name=code,
            base_url="https://enc.example/v1",
            api_key_cipher=cipher,
            priority=1,
            enabled=1,
        )
        stored = await llm_endpoint_repo.get_by_code(db, code)
        assert stored is not None
        row = await db.get(LlmEndpoint, stored.id)
        assert row is not None
        assert row.api_key_cipher == cipher
        assert row.api_key_cipher != "plain-secret"
        assert decrypt(row.api_key_cipher) == "plain-secret"
    finally:
        await llm_endpoint_repo.delete(db, code)
