"""工具级白名单、并发闸门与超时。"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import httpx
import pytest

import app.core.agent_runner as runner_mod
from app.core.agent_runner import AgentRunner
from app.main import create_app
from tests.conftest import authenticate_admin
from app.models.agent import Agent


class _Runner(AgentRunner):
    """隔离事件出口的测试 Runner。"""

    async def _emit(self, event_type, payload):
        """丢弃测试事件，只验证工具执行路径。"""
        return None


def _agent(concurrency: int = 4, timeout_sec: float = 1.0) -> Agent:
    """构造带 calc 工具策略的内存 Agent。"""
    return Agent(
        code=f"policy-{uuid4().hex[:8]}",
        name="policy",
        system_prompt="test",
        model="fake",
        base_url="http://invalid.test/v1",
        api_key_cipher="x",
        tools=["calc"],
        tool_policy={
            "calc": {
                "enabled": True,
                "max_concurrency": concurrency,
                "timeout_sec": timeout_sec,
            }
        },
    )


def _call(index: int) -> dict:
    """构造一个合法形态的 tool_call。"""
    return {
        "id": f"call-{index}",
        "type": "function",
        "function": {
            "name": "calc",
            "arguments": '{"expression":"1+1"}',
        },
    }


@pytest.mark.asyncio
async def test_disabled_tool_is_not_exposed_or_allowed():
    """enabled=false 同时收紧模型 schema 与服务端执行白名单。"""
    agent = _agent()
    agent.tool_policy["calc"]["enabled"] = False
    runner = _Runner(agent=agent, node_id="n", run_id=1, hub=None)
    assert runner._allowed_tools() == []


@pytest.mark.asyncio
async def test_same_tool_calls_run_concurrently_up_to_policy(monkeypatch):
    """同一轮的多个调用并行执行，同名工具受各自信号量限制。"""
    async def _slow(*_args, **_kwargs):
        """模拟耗时工具调用。"""
        await asyncio.sleep(0.08)
        return True, "ok"

    async def _no_audit(**_kwargs):
        """跳过节流测试中的审计写入。"""
        return None

    monkeypatch.setattr(runner_mod, "execute_tool", _slow)
    monkeypatch.setattr(runner_mod, "record_tool_audit", _no_audit)
    runner = _Runner(
        agent=_agent(concurrency=2),
        node_id="n",
        run_id=1,
        hub=None,
    )
    started = time.monotonic()
    outcomes = await asyncio.gather(
        runner._execute_one_tool_call(_call(1)),
        runner._execute_one_tool_call(_call(2)),
    )
    elapsed = time.monotonic() - started
    assert [item[2] for item in outcomes] == [True, True]
    assert elapsed < 0.15


@pytest.mark.asyncio
async def test_same_tool_concurrency_one_serializes(monkeypatch):
    """max_concurrency=1 时同名调用必须串行。"""
    async def _slow(*_args, **_kwargs):
        """模拟耗时工具调用。"""
        await asyncio.sleep(0.06)
        return True, "ok"

    async def _no_audit(**_kwargs):
        """跳过节流测试中的审计写入。"""
        return None

    monkeypatch.setattr(runner_mod, "execute_tool", _slow)
    monkeypatch.setattr(runner_mod, "record_tool_audit", _no_audit)
    runner = _Runner(
        agent=_agent(concurrency=1),
        node_id="n",
        run_id=1,
        hub=None,
    )
    started = time.monotonic()
    await asyncio.gather(
        runner._execute_one_tool_call(_call(1)),
        runner._execute_one_tool_call(_call(2)),
    )
    assert time.monotonic() - started >= 0.11


@pytest.mark.asyncio
async def test_tool_timeout_is_fed_back_not_fatal(monkeypatch):
    """超时转成工具失败结果，模型可自行改路。"""
    async def _hang(*_args, **_kwargs):
        """模拟超过策略时限的工具调用。"""
        await asyncio.sleep(0.2)
        return True, "late"

    audited = []

    async def _capture(**kwargs):
        """捕获超时审计载荷供断言。"""
        audited.append(kwargs)
        return None

    monkeypatch.setattr(runner_mod, "execute_tool", _hang)
    monkeypatch.setattr(runner_mod, "record_tool_audit", _capture)
    runner = _Runner(
        agent=_agent(timeout_sec=0.03),
        node_id="n",
        run_id=1,
        hub=None,
    )
    _, _, ok, output = await runner._execute_one_tool_call(_call(1))
    assert ok is False
    assert "执行超过" in output
    assert audited and audited[0]["failure_type"] == "timeout"


@pytest.mark.asyncio
async def test_agent_api_validates_tool_policy():
    """保存期拒绝未授权工具策略，接受合法策略。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as client:
            await authenticate_admin(client)
            code = f"policy-{uuid4().hex[:8]}"
            invalid = await client.post(
                "/api/agents",
                json={
                    "code": code,
                    "name": "p",
                    "system_prompt": "x",
                    "model": "m",
                    "base_url": "http://x",
                    "tools": ["calc"],
                    "tool_policy": {
                        "now": {"enabled": True},
                    },
                },
            )
            assert invalid.status_code == 400

            valid = await client.post(
                "/api/agents",
                json={
                    "code": code,
                    "name": "p",
                    "system_prompt": "x",
                    "model": "m",
                    "base_url": "http://x",
                    "tools": ["calc"],
                    "tool_policy": {
                        "calc": {
                            "enabled": False,
                            "max_concurrency": 2,
                            "timeout_sec": 3,
                        }
                    },
                },
            )
            assert valid.status_code == 201, valid.text
            assert valid.json()["tool_policy"]["calc"]["enabled"] is False
