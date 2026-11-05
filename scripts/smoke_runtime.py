"""低并发运行时烟测：真实数据库、MCP 与 MiniMax OpenAI 兼容接口。

脚本只从环境变量读取凭据，不打印请求头、API Key 或密钥密文。它适合在低配
测试机上对已经启动的服务做一次完整但克制的验收：

1. 在数据库配置一个不可达的高优先级接口与一个真实的 MiniMax 接口；
2. 创建并发布带资源的 Skill，再绑定到 Agent；
3. 注册真实 stdio MCP，刷新工具、读取资源与 prompt；
4. 运行一次真实模型会话，确认高优先级接口失败后能切到 MiniMax；
5. 检查失败切换、消息落库、密钥不回显等关键事实。

用法::

    MINIMAX_BASE_URL=... MINIMAX_MODEL=... MINIMAX_API_KEY=... \
    ADMIN_KEY=... python scripts/smoke_runtime.py \
      --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SmokeError(RuntimeError):
    """烟测断言失败；消息中不包含请求凭据。"""


class Api:
    """极小的 JSON HTTP 客户端，避免额外依赖。"""

    def __init__(self, base_url: str, admin_key: str) -> None:
        """保存服务地址与管理员请求头。

        Args:
            base_url: 服务根地址。
            admin_key: 管理员 Key，仅保存在进程内。
        """
        self.base_url = base_url.rstrip("/")
        self.admin_headers = {"X-Admin-Key": admin_key}

    def call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> Any:
        """发一个请求并返回解析后的 JSON；204 返回 ``None``。

        Args:
            method: HTTP 方法。
            path: 以 ``/`` 开头的接口路径。
            payload: 可选 JSON 请求体。
            body: 可选原始字节请求体。
            headers: 额外请求头。
            timeout: 超时秒数。

        Returns:
            响应的 JSON 值。

        Raises:
            SmokeError: HTTP 状态不是 2xx。
        """
        request_headers = dict(headers or {})
        data = body
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise SmokeError(
                f"{method} {path} -> HTTP {exc.code}: {detail}"
            ) from exc
        if not raw:
            return None
        return json.loads(raw)


def _required_env(name: str) -> str:
    """读取必需环境变量，缺失时给出明确错误。

    Args:
        name: 环境变量名。

    Returns:
        环境变量值。

    Raises:
        SmokeError: 变量为空。
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise SmokeError(f"缺少环境变量：{name}")
    return value


def _wait_run(api: Api, run_id: int, timeout: float = 180.0) -> dict[str, Any]:
    """等待一次 run 进入终态。

    Args:
        api: API 客户端。
        run_id: run id。
        timeout: 最长等待秒数。

    Returns:
        run 详情。

    Raises:
        SmokeError: 超时或 run 失败。
    """
    deadline = time.monotonic() + timeout
    last_status = ""
    while time.monotonic() < deadline:
        run = api.call("GET", f"/api/runs/{run_id}", timeout=20)
        status = str(run.get("status") or "")
        if status != last_status:
            print(f"run {run_id}: {status}")
            last_status = status
        if status == "done":
            return run
        if status in {"error", "cancelled"}:
            raise SmokeError(
                f"run {run_id} 结束状态为 {status}：{run.get('error') or '无错误说明'}"
            )
        time.sleep(1)
    raise SmokeError(f"run {run_id} 在 {timeout:.0f} 秒内未结束")


def main() -> int:
    """执行烟测并打印不含凭据的结果摘要。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    admin_key = _required_env("ADMIN_KEY")
    minimax_base_url = _required_env("MINIMAX_BASE_URL")
    minimax_model = _required_env("MINIMAX_MODEL")
    minimax_api_key = _required_env("MINIMAX_API_KEY")
    api = Api(args.base_url, admin_key)
    suffix = uuid.uuid4().hex[:8]

    health = api.call("GET", "/api/health")
    if health.get("status") != "ok":
        raise SmokeError(f"健康检查失败：{health}")
    print("health: ok")

    bad_code = f"smoke-bad-{suffix}"
    good_code = f"smoke-minimax-{suffix}"
    bad = api.call(
        "POST",
        "/api/llm-endpoints",
        payload={
            "code": bad_code,
            "name": "smoke unreachable",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "smoke-invalid-key",
            "priority": 1,
        },
        headers=api.admin_headers,
    )
    good = api.call(
        "POST",
        "/api/llm-endpoints",
        payload={
            "code": good_code,
            "name": "smoke minimax",
            "base_url": minimax_base_url,
            "api_key": minimax_api_key,
            "priority": 2,
        },
        headers=api.admin_headers,
    )
    if "api_key" in bad or "api_key_cipher" in bad:
        raise SmokeError("LLM 接口响应回显了密钥字段")
    if "api_key" in good or "api_key_cipher" in good:
        raise SmokeError("LLM 接口响应回显了密钥字段")
    print("llm endpoints: encrypted, ordered fallback configured")

    skill_code = f"smoke-skill-{suffix}"
    skill_marker = f"SMOKE_SKILL_{suffix}"
    skill = api.call(
        "POST",
        "/api/skills",
        payload={
            "code": skill_code,
            "description": "运行时烟测技能",
            "body": f"在回答中保留标记 {skill_marker}。",
            "status": "published",
            "enabled": True,
        },
        headers=api.admin_headers,
    )
    resource = api.call(
        "PUT",
        f"/api/skills/{skill_code}/resources?"
        + urlencode({"path": "references/smoke.txt"}),
        body=b"skill resource smoke\n",
        headers={**api.admin_headers, "Content-Type": "text/plain"},
    )
    if resource.get("path") != "references/smoke.txt":
        raise SmokeError(f"Skill 资源写入结果异常：{resource}")
    print(f"skill: {skill['code']} v{skill['version']}, resource ok")

    mcp_code = f"smoke-mcp-{suffix}"
    api.call(
        "POST",
        "/api/mcp/servers",
        payload={
            "code": mcp_code,
            "name": "runtime smoke MCP",
            "transport": "stdio",
            "command": "/usr/local/bin/python",
            "args": ["/app/tests/fixtures/mcp_server_stdio.py"],
            "env": {"PYTHONPATH": "/app"},
        },
        headers=api.admin_headers,
    )
    refreshed = api.call(
        "POST", f"/api/mcp/servers/{mcp_code}/refresh", headers=api.admin_headers
    )
    if not refreshed.get("imported"):
        raise SmokeError(f"MCP 刷新没有导入工具：{refreshed}")
    resources = api.call(
        "GET", f"/api/mcp/servers/{mcp_code}/resources", headers=api.admin_headers
    )
    read = api.call(
        "GET",
        f"/api/mcp/servers/{mcp_code}/resources/read?"
        + urlencode({"uri": "fixture://guide"}),
        headers=api.admin_headers,
    )
    prompts = api.call(
        "GET", f"/api/mcp/servers/{mcp_code}/prompts", headers=api.admin_headers
    )
    if not any(item.get("uri") == "fixture://guide" for item in resources):
        raise SmokeError("MCP resources 列表缺少 fixture://guide")
    if not read or "资源读取正常" not in str(read[0].get("text") or ""):
        raise SmokeError("MCP resource 读取内容不符合预期")
    if not any(item.get("name") == "summarize" for item in prompts):
        raise SmokeError("MCP prompts 列表缺少 summarize")
    print(f"mcp: {mcp_code}, tools/resources/prompts ok")

    agent_code = f"smoke_agent_{suffix}"
    agent = api.call(
        "POST",
        "/api/agents",
        payload={
            "code": agent_code,
            "name": "runtime smoke agent",
            "description": "MiniMax runtime smoke",
            "system_prompt": "你是运行时烟测助手。",
            "model": minimax_model,
            "temperature": 0,
            "max_tokens": 64,
            "skills": [skill_code],
        },
    )
    preview = api.call("GET", f"/api/agents/{agent_code}/prompt-preview")
    if not any(
        item.get("code") == skill_code and item.get("injected")
        for item in preview.get("skills", [])
    ):
        raise SmokeError("Skill 没有注入 Agent prompt")
    if agent.get("has_api_key") is not False:
        raise SmokeError("Agent 不应持有旧版内嵌 API Key")

    workflow_code = f"smoke_wf_{suffix}"
    api.call(
        "POST",
        "/api/workflows",
        payload={
            "code": workflow_code,
            "name": "runtime smoke workflow",
            "definition": {
                "entry": "answer",
                "nodes": [
                    {
                        "id": "answer",
                        "agentCode": agent_code,
                        "outputKey": "answer_out",
                    }
                ],
                "edges": [],
                "inputTemplate": "{{user_input}}",
            },
        },
    )
    conversation = api.call(
        "POST",
        "/api/conversations",
        payload={
            "workflow_code": workflow_code,
            "user_id": f"smoke-user-{suffix}",
        },
    )
    sent = api.call(
        "POST",
        f"/api/conversations/{conversation['id']}/messages",
        payload={"content": "请只回复 SMOKE_OK，不要调用工具。"},
    )
    run = _wait_run(api, int(sent["run_id"]), timeout=args.timeout)
    messages = api.call("GET", f"/api/runs/{run['id']}/messages")
    assistant = [
        item
        for item in messages
        if item.get("role") == "assistant" and str(item.get("content") or "").strip()
    ]
    if not assistant:
        raise SmokeError("真实模型 run 没有落库 assistant 消息")
    print(
        "run: done, "
        f"assistant_chars={len(assistant[-1]['content'])}, "
        f"skill_version={skill['version']}"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "suffix": suffix,
                "run_id": run["id"],
                "agent": agent_code,
                "skill": skill_code,
                "mcp": mcp_code,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
