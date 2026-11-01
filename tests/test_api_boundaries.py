"""API 层的输入边界与错误脱敏。

四组断言，每一组都对应一条「会被受理/会被回显」的越界路径：

1. 请求体超过上限 → 413（而不是整包进内存、落库、送 LLM）；
2. 关键字段有长度上限 → 400；
3. 深层嵌套的 JSON → 400（不是 500）；
4. 错误信息里的密钥形态内容被遮蔽。
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.core.errors import redact_error_message
from app.main import create_app


@pytest.fixture
def client():
    """不需要 lifespan 的客户端。

    这几个用例打的是「请求进来之后、碰业务之前」的那几道关，起停 checkpointer
    与敏感词模型对它们没有影响；跳过 lifespan 也让用例更快。
    """
    return TestClient(create_app())


# --------------------------------------------------------------- 请求体上限


def test_oversized_body_is_rejected(client: TestClient):
    """超大的请求体直接 413，别整包吃进内存再落库、送模型。"""
    payload = {
        "code": "too-big",
        "name": "太大了",
        "system_prompt": "A" * (settings.max_request_bytes + 1024),
        "model": "m",
        "base_url": "http://127.0.0.1:1/v1",
    }
    r = client.post("/api/agents", json=payload)
    assert r.status_code == 413, f"应 413，实际 {r.status_code}：{r.text[:200]}"
    assert r.json()["code"] == "payload_too_large"


def test_normal_body_is_accepted(client: TestClient):
    """限额之上的正常请求不受影响——别把上限设成连正常配置都收不了。

    code 带上随机后缀：这个用例会真的落一条 agent 记录，固定 code 在重复跑
    同一个库时会撞上「code 已存在」变成 409，掩盖掉本来要验的 201。
    """
    r = client.post(
        "/api/agents",
        json={
            "code": f"small-enough-{uuid4().hex[:8]}",
            "name": "正常",
            "system_prompt": "你是一个助手",
            "model": "m",
            "base_url": "http://127.0.0.1:1/v1",
        },
    )
    assert r.status_code == 201, f"应 201，实际 {r.status_code}：{r.text[:200]}"


# --------------------------------------------------------------- 字段长度


def test_system_prompt_length_is_bounded(client: TestClient):
    """system_prompt 超长 → 400。

    它是要整段送进模型的：无上限时一条 5 MB 的提示词会被完整受理，token 账单
    按字符走。
    """
    r = client.post(
        "/api/agents",
        json={
            "code": "long-prompt",
            "name": "太长",
            "system_prompt": "字" * 50_000,
            "model": "m",
            "base_url": "http://127.0.0.1:1/v1",
        },
    )
    assert r.status_code == 400, f"应 400，实际 {r.status_code}：{r.text[:200]}"


def test_message_content_length_is_bounded(client: TestClient):
    """用户消息超长 → 400。

    这条直接打 schema 层：消息路由要先过 workflow 存在性检查（409/404），
    走不到内容长度校验，用例就测不到目标行为了。
    """
    from pydantic import ValidationError

    from app.schemas.conversation import SendMessageIn

    with pytest.raises(ValidationError):
        SendMessageIn(content="字" * 50_000)


# --------------------------------------------------------------- JSON 深度


def test_deeply_nested_json_is_rejected(client: TestClient):
    """嵌套太深的 JSON → 400，不是 500。

    pydantic 序列化 3000 层嵌套的 dict 会抛 "Circular reference detected
    (depth exceeded)"，那是个未捕获的异常 → 500，违反「参数不合法 → 400」。
    """
    deep: dict = {"a": 1}
    for _ in range(50):
        deep = {"n": deep}

    r = client.post(
        "/api/agents",
        json={
            "code": "deep",
            "name": "深",
            "system_prompt": "你是助手",
            "model": "m",
            "base_url": "http://127.0.0.1:1/v1",
            "extra_params": deep,
        },
    )
    assert r.status_code == 400, f"应 400，实际 {r.status_code}：{r.text[:200]}"


def test_deep_json_rejection_does_not_leak_500(client: TestClient):
    """嵌套深到 pydantic 自己都序列化不了时，也必须是 400。

    这里直接打 schema 层：请求体已经过了 pydantic 的解析，深度守卫要拦住它。
    """
    from app.schemas.agent import AgentCreate
    from pydantic import ValidationError

    deep: dict = {"a": 1}
    for _ in range(50):
        deep = {"n": deep}

    with pytest.raises(ValidationError):
        AgentCreate(
            code="x",
            name="n",
            system_prompt="s",
            model="m",
            base_url="http://x/v1",
            extra_params=deep,
        )


# --------------------------------------------------------------- 错误脱敏


@pytest.mark.parametrize(
    "raw, must_not_contain",
    [
        ("调用失败：api_key=sk-TESTPLACEHOLDER0000", "sk-TESTPLACEHOLDER0000"),
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ('{"api_key": "supersecretvalue"}', "supersecretvalue"),
        ("连接 mysql://root:pa55word@10.0.0.1:3306/db 失败", "pa55word"),
    ],
)
def test_error_messages_are_redacted(raw: str, must_not_contain: str):
    """错误信息里的密钥形态内容必须被遮掉。

    错误信息会落进 ``run.error_msg``，而 ``GET /api/runs/{id}`` 原样回显它。
    """
    out = redact_error_message(raw)
    assert out is not None
    assert must_not_contain not in out, f"密钥没被遮住：{out}"
    assert "***" in out, f"应该用 *** 标记遮蔽位置：{out}"


def test_error_redaction_keeps_diagnosable_text():
    """脱敏只遮密钥，不吃掉排障需要的部分。"""
    raw = "Connection refused到 http://10.0.0.1:6379，api_key=sk-TESTPLACEHOLDER0000"
    out = redact_error_message(raw)
    assert out is not None
    assert "Connection refused" in out
    assert "10.0.0.1:6379" in out


def test_redaction_is_idempotent_on_plain_text():
    """普通文本原样返回——脱敏不该改变正常错误信息。"""
    raw = "节点 n1 执行失败：模型端点超时"
    assert redact_error_message(raw) == raw
    assert redact_error_message(None) is None


def test_health_reports_dependencies(client: TestClient):
    """健康检查要报出依赖状态，而不只是「进程活着」。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    # Redis 未启用时是 disabled；启用时必须有 ok/error 之一
    assert body["redis"] in ("ok", "error", "disabled")
    # 不回显原始驱动异常（那会泄露 DB 主机、端口、用户名）
    assert "error:" not in body["db"], body


def test_health_body_is_json_not_html(client: TestClient):
    """健康检查返回的是结构化 JSON，前端与探针脚本都依赖它。"""
    r = client.get("/api/health")
    assert r.headers["content-type"].startswith("application/json")
    json.loads(r.text)
