"""工具调用审计、脱敏、查询与保留期清理。"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.core.agent_runner as agent_runner_mod
from app.config.db import SessionLocal
from app.config.settings import settings
from app.core import tool_audit as audit_mod
from app.core.agent_runner import AgentResult, AgentRunner
from app.core.errors import SensitiveContentError
from app.core.tool_audit import (
    prepare_audit_arguments,
    prepare_audit_result,
    record_tool_audit,
)
from app.main import create_app
from app.models.agent import Agent
from app.models.base import utcnow
from app.models.tool_call_audit import ToolCallAudit
from app.repo.tool_audit_repo import tool_audit_repo
from tests.conftest import authenticate_admin_sync


def _run_id() -> int:
    """生成足够大的测试 run id，避免与 SQLite 小整数用例串记录。"""
    return 1_000_000_000 + uuid4().int % 1_000_000_000


async def _delete_run(run_id: int) -> None:
    """删除测试 run 的全部审计记录。"""
    async with SessionLocal() as db:
        await db.execute(
            sa.delete(ToolCallAudit).where(ToolCallAudit.run_id == run_id)
        )
        await db.commit()


def test_audit_arguments_are_redacted_without_losing_shape():
    """参数字段和字符串中的凭据都脱敏，同时保留可检索的 JSON 结构。"""
    safe, redacted, chars = prepare_audit_arguments(
        {
            "apiKey": "sk-plain-secret",
            "nested": {
                "password": "abc12345",
                "note": "Authorization: Bearer abcdefghijk",
            },
            "items": [{"access_token": "token-value"}],
        }
    )

    assert redacted is True
    assert chars > 0
    assert safe["apiKey"] == "***"
    assert safe["nested"]["password"] == "***"
    assert safe["items"][0]["access_token"] == "***"
    assert "abcdefghijk" not in safe["nested"]["note"]
    assert set(safe) == {"apiKey", "nested", "items"}


def test_audit_result_is_redacted_truncated_and_measured(monkeypatch):
    """结果先脱敏再截断；原始字符数与实际保存字节数分开记录。"""
    monkeypatch.setattr(settings, "tool_audit_max_result_chars", 12)
    stored, chars, size_bytes, truncated = prepare_audit_result(
        "clientSecret=client-value password=abcdefgh secret"
    )

    full_safe = "clientSecret=*** password=*** secret"
    assert stored == full_safe[:12]
    assert chars == len("clientSecret=client-value password=abcdefgh secret")
    assert size_bytes == len(stored.encode("utf-8"))
    assert truncated is True


@pytest.mark.asyncio
async def test_record_tool_audit_persists_redacted_metadata():
    """成功执行会生成可从 run/node/tool_call 关联的独立记录。"""
    run_id = _run_id()
    audit_id = await record_tool_audit(
        run_id=run_id,
        node_id="n1",
        tool_call_id="call-ok",
        tool_name="calc",
        arguments={"api_key": "secret-value", "expression": "1+1"},
        ok=True,
        output="结果 api_key=secret-value",
        duration_ms=17,
    )

    assert audit_id is not None
    async with SessionLocal() as db:
        row = await tool_audit_repo.get_by_id(db, audit_id)
        assert row is not None
        assert row.run_id == run_id
        assert row.tool_call_id == "call-ok"
        assert row.source == "builtin"
        assert row.arguments_json["api_key"] == "***"
        assert row.arguments_redacted == 1
        assert row.result_text == "结果 api_key=***"
        assert row.ok == 1
        assert row.duration_ms == 17
        assert row.expires_at > row.created_at
    await _delete_run(run_id)


@pytest.mark.asyncio
async def test_sensitive_audit_never_persists_blocked_body():
    """敏感输出被拦截时只落状态，不保存命中正文。"""
    run_id = _run_id()
    audit_id = await record_tool_audit(
        run_id=run_id,
        node_id="n-sensitive",
        tool_call_id="call-sensitive",
        tool_name="mcp__remote__echo",
        arguments="{}",
        ok=False,
        output="这段正文绝对不能落库",
        duration_ms=9,
        failure_type="sensitive_content",
    )

    assert audit_id is not None
    async with SessionLocal() as db:
        row = await tool_audit_repo.get_by_id(db, audit_id)
        assert row is not None
        assert row.source == "mcp"
        assert row.server == "remote"
        assert row.result_text is None
        assert row.result_chars == 0
        assert row.failure_type == "sensitive_content"
    await _delete_run(run_id)


@pytest.mark.asyncio
async def test_audit_write_failure_is_best_effort(monkeypatch):
    """审计持久化失败返回 None，不把异常抛给工具业务调用方。"""
    async def _fail(*_args, **_kwargs):
        """模拟数据库写入失败。"""
        raise RuntimeError("db down")

    monkeypatch.setattr(tool_audit_repo, "create", _fail)
    result = await record_tool_audit(
        run_id=_run_id(),
        node_id="n-fail",
        tool_call_id="call-fail",
        tool_name="calc",
        arguments="{}",
        ok=True,
        output="1",
        duration_ms=1,
    )

    assert result is None


class _AuditRunner(AgentRunner):
    """用固定模型结果驱动工具审计，不发起真实 LLM 请求。"""

    def __init__(self, *args, result: AgentResult, **kwargs) -> None:
        """保存工具调用轮次后要返回的模型结果。"""
        super().__init__(*args, **kwargs)
        self._next_result = result

    async def run(self, messages: list[dict]) -> AgentResult:
        """首次请求一个工具，工具处理后返回最终答案。"""
        if not getattr(self, "_tool_requested", False):
            self._tool_requested = True
            return self._next_result
        return AgentResult(text="完成")

    async def _emit(self, event_type, payload):
        """测试不需要 SSE 事件，吞掉事件出口。"""
        return None


def _audit_agent() -> Agent:
    """构造只允许 calc 的内存 Agent。"""
    return Agent(
        code="audit-runner",
        name="audit-runner",
        system_prompt="test",
        model="fake",
        base_url="http://invalid.test/v1",
        api_key_cipher="x",
        tools=["calc"],
    )


@pytest.mark.asyncio
async def test_agent_runner_audits_sensitive_tool_exception(monkeypatch):
    """统一工具闭环捕获敏感异常并留下不含正文的失败审计。"""
    run_id = _run_id()

    async def _blocked(*_args, **_kwargs):
        """模拟第三方工具结果命中敏感词。"""
        raise SensitiveContentError("内容被拦截", data={"excerpt": "不能落库"})

    monkeypatch.setattr(agent_runner_mod, "execute_tool", _blocked)
    runner = _AuditRunner(
        agent=_audit_agent(),
        node_id="node-sensitive",
        run_id=run_id,
        hub=None,
        result=AgentResult(
            tool_calls=[
                {
                    "id": "call-blocked",
                    "type": "function",
                    "function": {"name": "calc", "arguments": "{}"},
                }
            ]
        ),
    )

    with pytest.raises(SensitiveContentError):
        await runner.run_with_tools([{"role": "user", "content": "test"}])

    async with SessionLocal() as db:
        row = await db.scalar(
            sa.select(ToolCallAudit).where(ToolCallAudit.run_id == run_id)
        )
        assert row is not None
        assert row.failure_type == "sensitive_content"
        assert row.result_text is None
    await _delete_run(run_id)


def test_tool_audit_api_requires_admin_and_hides_result_in_list():
    """列表只给摘要，正文必须通过管理员单条详情接口读取。"""
    run_id = _run_id()
    candidate = ToolCallAudit(
        run_id=run_id,
        node_id="api-node",
        tool_call_id="api-call",
        tool_name="skill_exec",
        source="skill",
        arguments_json={"script": "scripts/run.py"},
        arguments_redacted=0,
        arguments_chars=25,
        result_text="可读结果",
        result_chars=4,
        result_bytes=12,
        result_truncated=0,
        ok=1,
        failure_type=None,
        duration_ms=5,
        expires_at=utcnow() + timedelta(days=1),
    )

    async def _seed() -> int:
        """直接插入一条测试审计并返回 id。"""
        async with SessionLocal() as db:
            db.add(candidate)
            await db.commit()
            await db.refresh(candidate)
            return candidate.id

    import asyncio

    audit_id = asyncio.run(_seed())
    try:
        client = TestClient(create_app())
        assert client.get(f"/api/tool-audits?run_id={run_id}").status_code == 401
        authenticate_admin_sync(client)

        listed = client.get(f"/api/tool-audits?run_id={run_id}")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["total"] == 1
        assert body["items"][0]["source"] == "skill"
        assert "result" not in body["items"][0]

        detail = client.get(f"/api/tool-audits/{audit_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["result"] == "可读结果"
        assert detail.json()["arguments"]["script"] == "scripts/run.py"
    finally:
        asyncio.run(_delete_run(run_id))


@pytest.mark.asyncio
async def test_cleanup_removes_only_expired_records():
    """保留期清理删除过期项，并保留仍未到期的记录。"""
    expired_run = _run_id()
    fresh_run = _run_id()
    async with SessionLocal() as db:
        expired = await tool_audit_repo.create(
            db,
            run_id=expired_run,
            node_id="old",
            tool_call_id="old",
            tool_name="calc",
            source="builtin",
            arguments_json={},
            arguments_redacted=0,
            arguments_chars=2,
            result_text="old",
            result_chars=3,
            result_bytes=3,
            result_truncated=0,
            ok=1,
            duration_ms=1,
            expires_at=utcnow() - timedelta(seconds=1),
        )
        fresh = await tool_audit_repo.create(
            db,
            run_id=fresh_run,
            node_id="new",
            tool_call_id="new",
            tool_name="calc",
            source="builtin",
            arguments_json={},
            arguments_redacted=0,
            arguments_chars=2,
            result_text="new",
            result_chars=3,
            result_bytes=3,
            result_truncated=0,
            ok=1,
            duration_ms=1,
            expires_at=utcnow() + timedelta(days=1),
        )

    try:
        assert await audit_mod.cleanup_expired_tool_audits() >= 1
        async with SessionLocal() as db:
            assert await db.get(ToolCallAudit, expired.id) is None
            assert await db.get(ToolCallAudit, fresh.id) is not None
    finally:
        await _delete_run(expired_run)
        await _delete_run(fresh_run)


@pytest.mark.asyncio
async def test_cleanup_task_can_start_and_stop(monkeypatch):
    """周期清理任务重复启动仍只有一个实例，停止后不留下后台任务。"""
    monkeypatch.setattr(settings, "tool_audit_enabled", True)
    monkeypatch.setattr(settings, "tool_audit_cleanup_enabled", True)
    monkeypatch.setattr(settings, "tool_audit_cleanup_interval_sec", 0.01)
    await audit_mod.stop_tool_audit_cleanup()

    audit_mod.start_tool_audit_cleanup()
    first = audit_mod._cleanup_task
    audit_mod.start_tool_audit_cleanup()
    assert audit_mod._cleanup_task is first

    await audit_mod.stop_tool_audit_cleanup()
    assert first is not None and first.cancelled()
    assert audit_mod._cleanup_task is None
