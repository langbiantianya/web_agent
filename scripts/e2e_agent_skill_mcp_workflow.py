"""端到端验收流程：MiniMax 端点 × 智能体 × 技能 × MCP × 工作流。

这份脚本是「**完整的测试流程**」的可执行版本：它不 mock 任何东西——真的调
MiniMax 的 OpenAI 兼容接口，真的起一个 stdio MCP 子进程，真的走一遍工作流
编排（多节点 + 人工审批 + SSE 断线续传）。

## 为什么脚本要直接读写数据库

管理端（admin-web）与客户端运行时是**分库**的（见
``deploy/docker-compose.stack.yml`` 的说明）：管理端在 PostgreSQL，运行时在
MySQL。于是「管理端配好的 MiniMax 端点」在运行时库里根本不存在——管理端
看到的配置与真正被调用的配置是两套数据。

脚本因此做两件事：

1. **同步**：把管理端库里配置好的 MiniMax 端点（含加密后的 Key）镜像到运行
   时库。两边共用同一个 ``SECRET_KEY``，密文可以原样搬运；搬完还要**验一次
   能不能解密**——不能解密就等于端点没配好。
2. **布场**：在运行时库里建好技能、MCP 服务器、智能体与工作流，然后从**用户
   视角**（HTTP + SSE）跑一次完整会话。

管理端不可达时脚本会退回 ``.env`` 里的 ``MINIMAX_*`` 环境变量，保证「没有
管理端也能验运行时」。

## 用法

在**客户端容器**里跑（脚本要 import 应用代码，也要连运行时库）::

    docker exec -w /app -e PYTHONPATH=/app webagent-client \\
        python scripts/e2e_agent_skill_mcp_workflow.py

常用参数::

    --base-url   客户端服务地址，默认 http://127.0.0.1:8000
    --llm-code   要使用的 LLM 端点 code，默认 minimax
    --keep       保留布场数据（默认跑完清理）
    --json       额外打印一行 JSON 报告（给 CI 采集）

退出码：0 = 全部通过；1 = 有用例失败；2 = 环境/配置问题，跑不起来。

**脚本不会打印任何密钥、Token 或密文。**
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.db import SessionLocal
from app.config.settings import settings
from app.core.crypto import decrypt, encrypt
from app.core.mcp.catalog import refresh_catalog
from app.core.mcp.manager import mcp_manager
from app.core.mcp.naming import build_qualified_name
from app.core.skill_context import SkillPrompt, build_skill_messages
from app.core.tools import execute_tool
from app.core.user_auth import (
    create_user_session,
    csrf_cookie_name,
    session_cookie_name,
)
from app.core.workflow_def import WorkflowDefinition
from app.models.skill import SkillResource
from app.models.user import AppUser
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.skill_repo import skill_repo
from app.repo.workflow_repo import workflow_repo

# ---------------------------------------------------------------- 固定标识

PREFIX = "e2e-selfcheck"
SKILL_CODE = f"{PREFIX}-skill"
MCP_CODE = f"{PREFIX}-mcp"
AGENT_CODE = f"{PREFIX}-agent"
WRITER_CODE = f"{PREFIX}-writer"
WF_CODE = f"{PREFIX}-wf"
E2E_USER_ID = "00000000-0000-4000-8000-00000000e2e0"
E2E_USERNAME = "e2e-selfcheck"

# MCP 工具的**限定名**由「服务器 code + 工具名」拼出来（§21.5）：
# ``mcp__<server_code>__<tool_name>``。
TOOL_ADD = build_qualified_name(MCP_CODE, "add")
TOOL_ECHO = build_qualified_name(MCP_CODE, "echo")
TOOL_FAIL = build_qualified_name(MCP_CODE, "always_fail")

# 技能脚本：studio 侧要执行的脚本资源（§22.8）。它跑在 studio 容器里——
# 没有凭据、不能出网，只负责把一个标记打到 stdout 上。
SCRIPT_PATH = "scripts/e2e_sum.py"
SCRIPT_MARK = "E2E_STUDIO_SUM:7"
SCRIPT_BODY = '''#!/usr/bin/env python3
"""端到端验收脚本。

它证明三件事：脚本正文真的随请求发到了 studio、studio 真的起了子进程、
stdout 真的回到了 web_agent 并回灌给了模型。
"""

print("E2E_STUDIO_SUM:7")
'''

# 技能正文是「行为指令」：模型只有拿到这段正文才会去调那个 MCP 工具。
# 于是「工具真的被调了」反过来证明「技能真的注入进提示词了」——这是本流程
# 里最能说明问题的一条断言。
#
# 技能、MCP、studio 在**同一次 run** 里一起用：一次调用证明「技能注入 →
# 模型按技能办事」，同时验证 MCP 工具与 studio 脚本两条工具链都通。
SKILL_BODY = f"""# 端到端验收技能

你必须按顺序做两件事，**一件都不能少**：

1. 调用 MCP 工具 `{TOOL_ADD}`（参数 a=1、b=1）；
2. 调用内置工具 `skill_exec`（参数 skill=`{SKILL_CODE}`、
   script=`{SCRIPT_PATH}`），在 studio 容器里执行技能脚本。

然后再回答用户。回答里必须原样包含 MCP 工具返回的 `E2E_SUM:2` 标记；
如果工具没有返回这个标记，就回答「未获得工具结果」，不要编造。
"""

AGENT_PROMPT = "你是端到端验收助手。严格按技能说明执行，不要自行发挥。"
WRITER_PROMPT = (
    "你是验收报告撰写员。把上游产出整理成一句话结论，"
    "不要调用任何工具，不要寒暄。"
)

WORKFLOW_DEF = {
    "entry": "collect",
    "nodes": [
        {
            "id": "collect",
            "type": "agent",
            "agentCode": AGENT_CODE,
            "outputKey": "analysis",
        },
        {
            "id": "gate",
            "type": "approval",
            "label": "人工确认",
            "prompt": "请确认端到端自检结果是否可以进入汇总节点。",
            "requiredApprovals": 1,
            "approvalTimeoutSec": 0,
            "approvalTimeoutAction": "reject",
        },
        {
            "id": "report",
            "type": "agent",
            "agentCode": WRITER_CODE,
            "outputKey": "answer",
        },
    ],
    "edges": [
        {"from": "collect", "to": "gate"},
        {"from": "gate", "to": "report"},
    ],
    "inputTemplate": "用户问题：{{user_input}}\n\n上游产出：\n{{upstream}}",
}

# ---------------------------------------------------------------- 报告


@dataclass
class Case:
    """一条用例的结果。"""

    cid: str
    name: str
    status: str  # pass | fail | skip
    detail: str = ""


@dataclass
class Report:
    """用例集合与汇总。"""

    cases: list[Case] = field(default_factory=list)

    def add(self, cid: str, name: str, ok: bool, detail: str = "") -> Case:
        """记录一条通过/失败的用例。

        Args:
            cid: 用例编号。
            name: 用例名。
            ok: 是否通过。
            detail: 补充说明（不含凭据）。

        Returns:
            记录下来的用例。
        """
        case = Case(
            cid=cid, name=name, status="pass" if ok else "fail", detail=detail
        )
        self.cases.append(case)
        return case

    def skip(self, cid: str, name: str, reason: str) -> Case:
        """记录一条被跳过的用例。

        Args:
            cid: 用例编号。
            name: 用例名。
            reason: 跳过原因。

        Returns:
            记录下来的用例。
        """
        case = Case(cid=cid, name=name, status="skip", detail=reason)
        self.cases.append(case)
        return case

    @property
    def failed(self) -> list[Case]:
        """失败用例列表。"""
        return [c for c in self.cases if c.status == "fail"]

    @property
    def passed(self) -> int:
        """通过数量。"""
        return sum(1 for c in self.cases if c.status == "pass")

    @property
    def skipped(self) -> int:
        """跳过数量。"""
        return sum(1 for c in self.cases if c.status == "skip")

    def render(self) -> str:
        """渲染成给人看的表格。"""
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
        lines = [
            "",
            "=" * 78,
            "端到端验收报告：MiniMax × 智能体 × 技能 × MCP × 工作流",
            "=" * 78,
        ]
        for case in self.cases:
            lines.append(f"[{mark[case.status]:4}] {case.cid} {case.name}")
            if case.detail:
                lines.append(f"        {case.detail}")
        total = len(self.cases)
        lines.append("-" * 78)
        lines.append(
            f"总计 {total} 条：通过 {self.passed}，"
            f"失败 {len(self.failed)}，跳过 {self.skipped}"
        )
        lines.append("=" * 78)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的结构。"""
        return {
            "total": len(self.cases),
            "passed": self.passed,
            "failed": len(self.failed),
            "skipped": self.skipped,
            "cases": [
                {
                    "id": c.cid,
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                }
                for c in self.cases
            ],
        }


def log(msg: str) -> None:
    """打印一行进度。"""
    print(f"  · {msg}", flush=True)


# ---------------------------------------------------------------- 端点同步


async def _admin_db_fetch_llm(code: str) -> dict[str, Any] | None:
    """从管理端库（PostgreSQL）读一条 LLM 端点配置。

    Args:
        code: 端点 code。

    Returns:
        配置字典（含密文），读不到时返回 ``None``。
    """
    url = (settings.admin_database_url or "").strip()
    if not url:
        return None
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        "select code, name, base_url, model, api_key_cipher, "
                        "priority, enabled from llm_endpoint where code = :code"
                    ),
                    {"code": code},
                )
            ).mappings().first()
        if row is None:
            return None
        return dict(row)
    finally:
        await engine.dispose()


async def sync_llm_endpoint(
    db: AsyncSession, code: str
) -> tuple[dict[str, Any], str]:
    """把管理端配置好的 LLM 端点镜像到运行时库。

    镜像的是**密文**：两边共用同一个 ``SECRET_KEY``，密文原样搬过去就能解密。
    管理端库不可达时退回 ``.env`` 的 ``MINIMAX_*``（本地没有 Key 时也能建出
    一条空 Key 的配置，流程会在真正调用时明确失败）。

    Args:
        db: 运行时库会话。
        code: 端点 code。

    Returns:
        ``(配置, 来源说明)``。
    """
    source = "env"
    payload = await _admin_db_fetch_llm(code)
    if payload:
        source = "admin-db"
    else:
        payload = {
            "code": code,
            "name": "MiniMax（来自 .env）",
            "base_url": settings.minimax_base_url,
            "model": settings.minimax_model,
            "api_key_cipher": encrypt(settings.minimax_api_key),
            "priority": 100,
            "enabled": 1,
        }
    row = await llm_endpoint_repo.get_by_code(db, code)
    if row is None:
        await llm_endpoint_repo.create(
            db,
            code=payload["code"],
            name=payload["name"],
            base_url=payload["base_url"],
            model=payload["model"],
            api_key_cipher=payload["api_key_cipher"],
            priority=int(payload.get("priority") or 100),
            enabled=1,
        )
    else:
        await llm_endpoint_repo.update(
            db,
            code,
            {
                "name": payload["name"],
                "base_url": payload["base_url"],
                "model": payload["model"],
                "api_key_cipher": payload["api_key_cipher"],
                "priority": int(payload.get("priority") or 100),
                "enabled": 1,
            },
            bump_version=False,
        )
    fresh = await llm_endpoint_repo.get_by_code(db, code)
    assert fresh is not None
    return {
        "code": fresh.code,
        "name": fresh.name,
        "base_url": fresh.base_url,
        "model": fresh.model,
        "priority": fresh.priority,
        "enabled": fresh.enabled,
    }, source

# ---------------------------------------------------------------- 布场


async def ensure_skill(db: AsyncSession) -> dict[str, Any]:
    """建（或更新）验收用技能，并挂一个资源文件。

    Args:
        db: 运行时库会话。

    Returns:
        技能摘要。
    """
    digest = hashlib.sha256(SKILL_BODY.encode("utf-8")).hexdigest()
    row = await skill_repo.get_by_code(db, SKILL_CODE, tenant_id="default")
    if row is None:
        row = await skill_repo.create(
            db,
            code=SKILL_CODE,
            name=SKILL_CODE,
            description="端到端验收技能：强制走一次 MCP 工具调用",
            license="",
            compatibility="",
            metadata_={"e2e": True},
            allowed_tools=f"{TOOL_ADD},{TOOL_ECHO}",
            body=SKILL_BODY,
            origin="manual",
            content_hash=digest,
            enabled=1,
            status="published",
        )
    else:
        row.body = SKILL_BODY
        row.content_hash = digest
        row.enabled = 1
        row.status = "published"
        await db.commit()
        await db.refresh(row)

    resource_path = "references/e2e.md"
    exists = await db.scalar(
        sa.select(SkillResource.id).where(
            SkillResource.skill_id == row.id,
            SkillResource.path == resource_path,
        )
    )
    if exists is None:
        payload = SKILL_BODY.encode("utf-8")
        db.add(
            SkillResource(
                skill_id=row.id,
                path=resource_path,
                mime_type="text/markdown",
                is_text=1,
                content=payload,
                size_bytes=len(payload),
            )
        )
        await db.commit()

    # 脚本资源：studio 侧要执行的就是它。路径必须在 scripts/ 下（§22.8.3）。
    script_exists = await db.scalar(
        sa.select(SkillResource.id).where(
            SkillResource.skill_id == row.id,
            SkillResource.path == SCRIPT_PATH,
        )
    )
    if script_exists is None:
        payload = SCRIPT_BODY.encode("utf-8")
        db.add(
            SkillResource(
                skill_id=row.id,
                path=SCRIPT_PATH,
                mime_type="text/x-python",
                is_text=1,
                content=payload,
                size_bytes=len(payload),
            )
        )
        await db.commit()
    return {
        "code": row.code,
        "version": row.version,
        "status": row.status,
        "enabled": bool(row.enabled),
        "resource": resource_path,
        "script": SCRIPT_PATH,
    }


async def ensure_mcp_server(
    db: AsyncSession, script_path: str
) -> dict[str, Any]:
    """建（或更新）验收用 MCP 服务器，并刷新工具目录。

    Args:
        db: 运行时库会话。
        script_path: MCP 服务器脚本在容器内的绝对路径。

    Returns:
        服务器摘要（含发现的工具名）。
    """
    from app.core.mcp.transport import McpTransportError, check_command_allowed

    command = "/usr/local/bin/python"
    try:
        check_command_allowed(command)
    except McpTransportError as exc:
        raise RuntimeError(
            "MCP_ALLOWED_COMMAND_PREFIXES 没放开 stdio 命令，验收流程跑不了："
            f"{exc}"
        ) from exc

    row = await mcp_server_repo.get_by_code(db, MCP_CODE, tenant_id="default")
    if row is None:
        row = await mcp_server_repo.create(
            db,
            code=MCP_CODE,
            tenant_id="default",
            name="端到端验收 MCP",
            transport="stdio",
            command=command,
            args=[script_path],
            env=None,
            allow_tools=None,
            timeout_sec=30,
            enabled=1,
        )
    else:
        row.command = command
        row.args = [script_path]
        row.enabled = 1
        await db.commit()
        await db.refresh(row)

    result = await refresh_catalog(db, row)
    tools = await mcp_tool_repo.list_by_server(db, row.id)
    return {
        "code": row.code,
        "transport": row.transport,
        "imported": list(result.imported),
        "tools": [t.qualified_name for t in tools],
    }


async def ensure_agents(
    db: AsyncSession, llm: dict[str, Any], tools: list[str]
) -> None:
    """建（或更新）两个验收用智能体。

    Args:
        db: 运行时库会话。
        llm: 目标 LLM 端点。
        tools: 允许模型调用的工具名（限定名）。
    """
    specs = [
        {
            "code": AGENT_CODE,
            "name": "端到端验收助手",
            "system_prompt": AGENT_PROMPT,
            "tools": tools,
            "skills": [SKILL_CODE],
        },
        {
            "code": WRITER_CODE,
            "name": "验收报告撰写员",
            "system_prompt": WRITER_PROMPT,
            "tools": [],
            "skills": [],
        },
    ]
    for spec in specs:
        row = await agent_repo.get_by_code(db, spec["code"])
        fields = {
            "code": spec["code"],
            "name": spec["name"],
            "description": "端到端验收流程专用",
            "system_prompt": spec["system_prompt"],
            "llm_endpoint_code": llm["code"],
            "model": llm["model"] or "MiniMax-M2.7",
            "base_url": "",
            "api_key_cipher": "",
            "temperature": 0.2,
            "max_tokens": 1024,
            "tools": spec["tools"],
            "skills": spec["skills"],
            "enabled": 1,
        }
        if row is None:
            await agent_repo.create(db, **fields)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
            await db.commit()
            await db.refresh(row)


async def ensure_workflow(db: AsyncSession) -> None:
    """建（或更新）验收用工作流。

    Args:
        db: 运行时库会话。
    """
    WorkflowDefinition.model_validate(WORKFLOW_DEF)  # 配置期校验，跑之前先过一遍
    row = await workflow_repo.get_by_code(db, WF_CODE)
    if row is None:
        await workflow_repo.create(
            db,
            code=WF_CODE,
            name="端到端验收工作流",
            description="collect(技能+MCP) → gate(审批) → report(汇总)",
            definition=WORKFLOW_DEF,
            enabled=1,
        )
    else:
        row.definition = WORKFLOW_DEF
        row.enabled = 1
        await db.commit()
        await db.refresh(row)


async def ensure_user(db: AsyncSession) -> tuple[str, str]:
    """建（或复用）验收用终端用户，返回会话 token 与 CSRF token。

    Args:
        db: 运行时库会话。

    Returns:
        ``(session_token, csrf_token)``。
    """
    user = await db.get(AppUser, E2E_USER_ID)
    if user is None:
        db.add(
            AppUser(
                id=E2E_USER_ID,
                tenant_id="default",
                username=E2E_USERNAME,
                email=f"{E2E_USERNAME}@example.test",
                status="active",
            )
        )
        await db.commit()
    token, csrf = await create_user_session(db, E2E_USER_ID)
    await db.commit()
    return token, csrf

# ---------------------------------------------------------------- SSE


class Streamer:
    """一次 run 的 SSE 订阅器。

    刻意做成「可断开、可续传」：验收流程要在流中间**主动断开一次**，再带
    ``last_seq`` 重连，然后检查服务端有没有把已经发过的 seq 再发一遍。
    """

    def __init__(
        self, client: httpx.AsyncClient, base_url: str, run_id: int
    ) -> None:
        """记住客户端与 run。

        Args:
            client: HTTP 客户端。
            base_url: 服务根地址。
            run_id: run id。
        """
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.last_seq = 0
        self.events: list[tuple[str, int | None, dict[str, Any]]] = []
        self.reconnects = 0

    async def ticket(self) -> str:
        """换一张新的 SSE 票据。

        Returns:
            票据字符串。
        """
        resp = await self.client.post(
            f"{self.base_url}/api/auth/sse-ticket", json={"run_id": self.run_id}
        )
        resp.raise_for_status()
        return str(resp.json()["ticket"])

    async def pump(
        self,
        stop_when: Any,
        timeout: float = 240.0,
        reconnect: bool = False,
    ) -> list[tuple[str, int | None, dict[str, Any]]]:
        """订阅事件流，直到 ``stop_when`` 命中或流结束。

        Args:
            stop_when: 收到事件后调用，返回 True 即停止。
            timeout: 最长等待秒数。
            reconnect: 是否是「重连」这一次（用于统计）。

        Returns:
            本次订阅收到的事件列表。
        """
        if reconnect:
            self.reconnects += 1
        ticket = await self.ticket()
        url = (
            f"{self.base_url}/api/runs/{self.run_id}/stream"
            f"?ticket={ticket}&last_seq={self.last_seq}"
        )
        collected: list[tuple[str, int | None, dict[str, Any]]] = []
        deadline = time.monotonic() + timeout
        async with self.client.stream("GET", url) as resp:
            resp.raise_for_status()
            event_name: str | None = None
            event_id: int | None = None
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                if time.monotonic() > deadline:
                    break
                if line == "":
                    if event_name is not None:
                        raw = "\n".join(data_lines)
                        try:
                            data = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            data = {"raw": raw}
                        collected.append((event_name, event_id, data))
                        self.events.append((event_name, event_id, data))
                        if event_id is not None and event_id > self.last_seq:
                            self.last_seq = event_id
                        if stop_when and stop_when(event_name, data):
                            break
                    event_name = None
                    event_id = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("id:"):
                    try:
                        event_id = int(line[len("id:") :].strip())
                    except ValueError:
                        event_id = None
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
        return collected


# ---------------------------------------------------------------- 清理


async def cleanup(conv_id: int) -> None:
    """清理布场数据。

    Args:
        conv_id: 会话 id。
    """
    log("清理布场数据…")
    async with SessionLocal() as db:
        await db.execute(
            sa.text("delete from message where conversation_id = :cid"),
            {"cid": conv_id},
        )
        await db.execute(
            sa.text(
                "delete from run_event where run_id in "
                "(select id from run where conversation_id = :cid)"
            ),
            {"cid": conv_id},
        )
        await db.execute(
            sa.text(
                "delete from run_step where run_id in "
                "(select id from run where conversation_id = :cid)"
            ),
            {"cid": conv_id},
        )
        await db.execute(
            sa.text("delete from run where conversation_id = :cid"), {"cid": conv_id}
        )
        await db.execute(
            sa.text("delete from conversation where id = :cid"), {"cid": conv_id}
        )
        await db.execute(
            sa.text(
                "delete from mcp_tool where server_id in "
                "(select id from mcp_server where code = :code)"
            ),
            {"code": MCP_CODE},
        )
        await db.execute(
            sa.text("delete from mcp_server where code = :code"), {"code": MCP_CODE}
        )
        await db.execute(
            sa.text(
                "delete from skill_resource where skill_id in "
                "(select id from skill where code = :code)"
            ),
            {"code": SKILL_CODE},
        )
        await db.execute(
            sa.text("delete from skill where code = :code"), {"code": SKILL_CODE}
        )
        await db.execute(
            sa.text("delete from agent where code in (:a, :b)"),
            {"a": AGENT_CODE, "b": WRITER_CODE},
        )
        await db.execute(
            sa.text("delete from workflow where code = :code"), {"code": WF_CODE}
        )
        await db.execute(
            sa.text("delete from user_session where user_id = :uid"),
            {"uid": E2E_USER_ID},
        )
        await db.commit()

# ---------------------------------------------------------------- 主流程


async def run_flow(args: argparse.Namespace, report: Report) -> int:
    """跑完整流程。

    Args:
        args: 命令行参数。
        report: 报告收集器。

    Returns:
        进程退出码。
    """
    base_url = args.base_url.rstrip("/")
    llm_code = args.llm_code

    async with SessionLocal() as db:
        # ---------------------------------------------------------- 端点同步
        log(f"同步 LLM 端点「{llm_code}」到运行时库…")
        try:
            llm, source = await sync_llm_endpoint(db, llm_code)
        except Exception as exc:  # noqa: BLE001
            report.add(
                "E2E-01", "LLM 端点同步到运行时库", False, f"同步失败：{exc}"
            )
            return 2
        report.add(
            "E2E-01",
            "LLM 端点已同步到运行时库",
            True,
            f"来源={source}，地址={llm['base_url']}，模型={llm['model']}",
        )

        # 登录供应商：浏览器端要能登录客户端控制台，运行时库里就得有一个
        # **指得着的** OIDC 供应商（管理端与运行时分库）。
        if args.skip_oidc:
            report.skip("E2E-00", "OIDC 登录供应商已同步", "--skip-oidc")
        else:
            try:
                oidp, oidp_source = await sync_oidc_provider(db, args.oidp_code)
            except Exception as exc:  # noqa: BLE001
                oidp, oidp_source = None, f"error:{exc}"
            report.add(
                "E2E-00",
                "OIDC 登录供应商已同步到运行时库",
                bool(oidp),
                f"来源={oidp_source}，issuer={oidp['issuer'] if oidp else '无'}"
                "（浏览器端登录的前提）",
            )

        # 端点密钥必须**能被运行时解开**——解不开就等于没配。
        row = await llm_endpoint_repo.get_by_code(db, llm_code)
        plain = decrypt(row.api_key_cipher) if row else ""
        report.add(
            "E2E-02",
            "端点密文在运行时可解密",
            bool(plain),
            f"密钥长度={len(plain)}（不打印内容）"
            if plain
            else "解不开：SECRET_KEY 与加密时不一致？",
        )

        # ---------------------------------------------------------- 布场
        log("布场：技能 / MCP / 智能体 / 工作流…")
        skill = await ensure_skill(db)
        report.add(
            "E2E-03",
            "技能已发布",
            skill["status"] == "published" and skill["enabled"],
            f"{skill['code']} v{skill['version']} 状态={skill['status']} "
            f"资源={skill['resource']}",
        )

        # 技能正文真的会被拼成一条 system 消息吗？用编排器同一个拼装函数验。
        skill_rows = await skill_repo.list_by_codes(db, [SKILL_CODE])
        plan = build_skill_messages(
            [
                SkillPrompt(
                    code=s.code, name=s.name, version=s.version, body=s.body
                )
                for s in skill_rows
            ],
            budget=settings.skill_prompt_budget,
            max_count=settings.skill_max_count,
        )
        injected = SKILL_CODE in plan.codes and any(
            TOOL_ADD in (m.get("content") or "") for m in plan.messages
        )
        report.add(
            "E2E-04",
            "技能正文注入到提示词",
            injected,
            f"注入={plan.codes or '无'}，跳过={plan.skipped or '无'}",
        )

        try:
            mcp = await ensure_mcp_server(db, args.mcp_script)
        except Exception as exc:  # noqa: BLE001
            report.add("E2E-05", "MCP 服务器已注册且工具目录已发现", False, str(exc))
            mcp = {"tools": []}
        tools_ok = TOOL_ADD in mcp["tools"] and TOOL_ECHO in mcp["tools"]
        report.add(
            "E2E-05",
            "MCP 服务器已注册且工具目录已发现",
            tools_ok,
            f"{mcp['code']} 传输={mcp['transport']} 导入={mcp['imported']}",
        )

        # skill_exec 是**可选工具**：只有 studio 可用时才注册（§22.8.6）。
        # 它要显式列进智能体的工具里，模型才看得见。
        await ensure_agents(db, llm, [TOOL_ADD, TOOL_ECHO, "skill_exec"])
        await ensure_workflow(db)
        log("布场完成。")

        # ---------------------------------------------------------- 工具直调
        try:
            ok, text = await execute_tool(
                TOOL_ADD, {"a": 2, "b": 40}, allowed=[TOOL_ADD]
            )
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"异常：{exc}"
        report.add(
            "E2E-06",
            "MCP 工具可真实调用（真起子进程）",
            ok and "E2E_SUM:42" in text,
            f"结果摘要={text[:80]}",
        )

        try:
            ok_fail, text_fail = await execute_tool(
                TOOL_FAIL, {}, allowed=[TOOL_FAIL]
            )
        except Exception as exc:  # noqa: BLE001
            ok_fail, text_fail = False, f"异常：{exc}"
        report.add(
            "E2E-07",
            "失败工具映射为错误结果而不是中断节点",
            (not ok_fail) and bool(text_fail),
            f"错误说明={text_fail[:80]}",
        )

        token, csrf = await ensure_user(db)

    # ---------------------------------------------------------- 用户视角跑一次
    headers = {
        "Cookie": f"{session_cookie_name()}={token}; {csrf_cookie_name()}={csrf}",
        "X-CSRF-Token": csrf,
    }
    async with httpx.AsyncClient(
        base_url=base_url, timeout=60.0, headers=headers
    ) as client:
        health = await client.get("/api/health")
        report.add(
            "E2E-08",
            "客户端服务健康",
            health.status_code == 200,
            f"HTTP {health.status_code}",
        )

        conv_resp = await client.post(
            "/api/conversations", json={"workflow_code": WF_CODE}
        )
        if conv_resp.status_code != 201:
            report.add(
                "E2E-09",
                "会话已创建",
                False,
                f"HTTP {conv_resp.status_code}：{conv_resp.text[:200]}",
            )
            return 1
        conv_id = int(conv_resp.json()["id"])
        report.add("E2E-09", "会话已创建", True, f"conversation_id={conv_id}")

        send_resp = await client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"content": "请按技能说明完成一次端到端自检。"},
        )
        if send_resp.status_code != 202:
            report.add(
                "E2E-10",
                "消息已提交并触发 run",
                False,
                f"HTTP {send_resp.status_code}：{send_resp.text[:200]}",
            )
            return 1
        run_id = int(send_resp.json()["run_id"])
        report.add("E2E-10", "消息已提交并触发 run", True, f"run_id={run_id}")

        streamer = Streamer(client, base_url, run_id)

        # 第一段：收到第一个 token 就**主动断开**——后面要用 last_seq 续上。
        first = await streamer.pump(
            stop_when=lambda ev, _d: ev in {"token", "done", "error"},
            timeout=args.timeout,
        )
        saw_token = any(ev == "token" for ev, _s, _d in first)
        report.add(
            "E2E-11",
            "第一段流收到模型吐字",
            saw_token,
            f"第一段事件数={len(first)}，last_seq={streamer.last_seq}",
        )

        # 第二段：带 last_seq 重连。服务端只应补发 seq 更大的事件。
        before = streamer.last_seq
        second = await streamer.pump(
            stop_when=lambda ev, _d: ev == "approval_required",
            timeout=args.timeout,
            reconnect=True,
        )
        duplicated = [s for _e, s, _d in second if s is not None and s <= before]
        report.add(
            "E2E-12",
            "断线后可凭 last_seq 续传且无重复",
            (not duplicated) and bool(second),
            f"重连前 last_seq={before}，重连后={streamer.last_seq}，"
            f"重复事件={duplicated or '无'}",
        )

        # 审批：流程挂起 → 决策 → 继续。
        approval_seen = any(
            ev == "approval_required" for ev, _s, _d in streamer.events
        )
        if approval_seen:
            # 服务端已经会等编排器把 run 状态落上（见 app/api/routes/run.py 的
            # _await_waiting），所以正常路径是**第一次就 200**。这里保留重试
            # 只是兜底：万一审批被并发决策掉，409 是该有的答案。
            appr = None
            attempts = 0
            for _ in range(8):
                attempts += 1
                appr = await client.post(
                    f"/api/runs/{run_id}/approval",
                    json={"approved": True, "comment": "端到端验收：批准"},
                )
                if appr.status_code != 409:
                    break
                await asyncio.sleep(0.3)
            report.add(
                "E2E-13",
                "审批节点挂起并接受决策",
                appr.status_code == 200,
                f"HTTP {appr.status_code}（尝试 {attempts} 次）",
            )
        else:
            report.add(
                "E2E-13",
                "审批节点挂起并接受决策",
                False,
                "没有收到 approval_required 事件",
            )

        await streamer.pump(
            stop_when=lambda ev, _d: ev in {"done", "error"},
            timeout=args.timeout,
            reconnect=True,
        )
        done = [d for ev, _s, d in streamer.events if ev == "done"]
        errored = [d for ev, _s, d in streamer.events if ev == "error"]
        status = str(done[-1].get("status")) if done else ""
        report.add(
            "E2E-14",
            "run 跑到终态",
            bool(done) and not errored,
            f"status={status or '无 done 事件'}，"
            f"错误={errored[-1] if errored else '无'}",
        )

        # 节点顺序
        starts = [
            d.get("node") for ev, _s, d in streamer.events if ev == "node_start"
        ]
        ends = [d.get("node") for ev, _s, d in streamer.events if ev == "node_end"]
        expected = ["collect", "report"]  # 审批节点不调模型，不发 node_start
        report.add(
            "E2E-15",
            "工作流节点按定义顺序执行",
            starts[: len(expected)] == expected
            and ends[: len(expected)] == expected,
            f"node_start={starts}，node_end={ends}",
        )

        # 工具调用
        calls = [
            tc
            for ev, _s, d in streamer.events
            if ev == "tool_call"
            for tc in (d.get("toolCalls") or [])
        ]
        called = [((tc.get("function") or {}).get("name")) for tc in calls]
        results = [d for ev, _s, d in streamer.events if ev == "tool_result"]
        result_tools = [str(d.get("tool") or "") for d in results]
        report.add(
            "E2E-16",
            "模型在节点内真实调用了 MCP 工具",
            TOOL_ADD in called and TOOL_ADD in result_tools,
            f"调用={called}，返回={result_tools}",
        )
        summary = " ".join(str(d.get("summary") or "") for d in results)
        report.add(
            "E2E-17",
            "工具结果回灌到模型上下文",
            "E2E_SUM" in summary,
            f"结果摘要片段={summary[:120]}",
        )

        # seq 连续性
        seqs = [s for _e, s, _d in streamer.events if s is not None]
        strictly_increasing = all(b > a for a, b in zip(seqs, seqs[1:]))
        report.add(
            "E2E-18",
            "事件 seq 严格递增且无重复",
            strictly_increasing,
            f"事件数={len(seqs)}，首={seqs[0] if seqs else None}，"
            f"末={seqs[-1] if seqs else None}",
        )

        # 最终消息
        msgs = await client.get(f"/api/runs/{run_id}/messages")
        rows = msgs.json() if msgs.status_code == 200 else []
        assistants = [
            m
            for m in rows
            if m.get("role") == "assistant" and (m.get("content") or "").strip()
        ]
        report.add(
            "E2E-19",
            "最终 assistant 消息已落库",
            bool(assistants),
            f"消息数={len(rows)}，assistant 正文长度="
            f"{len(assistants[-1]['content']) if assistants else 0}",
        )

        # 工具审计。``/api/tool-audits`` 是管理员接口（审计数据含参数与结果
        # 摘要，不对终端用户开放），所以这里直接查库核对。
        async with SessionLocal() as db:
            audit_rows = (
                await db.execute(
                    sa.text(
                        "select tool_name, source, ok from tool_call_audit "
                        "where run_id = :rid"
                    ),
                    {"rid": run_id},
                )
            ).mappings().all()
        mcp_audits = [
            a for a in audit_rows if str(a.get("tool_name") or "") == TOOL_ADD
        ]
        report.add(
            "E2E-20",
            "工具调用审计已落库",
            bool(mcp_audits),
            f"审计条数={len(audit_rows)}，命中 MCP 工具={len(mcp_audits)}",
        )

        # ---- studio：脚本执行这一段 --------------------------------
        #
        # studio 是独立容器（无凭据、只读、禁出网）。这里先问它的健康，
        # 再直接跑一次技能脚本，最后看模型**有没有**在 run 里真的调了它。
        from app.core.skill_exec import exec_skill_script
        from app.core.studio import configure_studio, get_studio_client

        # 脚本进程**不是** web_agent 主服务：主服务在启动时配置过 studio
        # （init_studio），这个独立进程没有——所以这里自己配一次，
        # 否则 get_studio_client() 永远是 None，会误判成「studio 没部署」。
        client_obj = get_studio_client() or configure_studio(
            settings.studio_exec_addr
        )
        if client_obj is None:
            report.add(
                "E2E-21",
                "studio 脚本执行服务已连接",
                False,
                "studio 未配置（STUDIO_EXEC_ADDR 为空）",
            )
        else:
            try:
                health = await client_obj.health(timeout=5.0)
                report.add(
                    "E2E-21",
                    "studio 脚本执行服务已连接",
                    bool(health.ready),
                    f"版本={health.version}，就绪={health.ready}，"
                    f"运行时={list(health.languages)}，"
                    f"档位={[p.name for p in health.profiles]}",
                )
            except Exception as exc:  # noqa: BLE001
                report.add(
                    "E2E-21",
                    "studio 脚本执行服务已连接",
                    False,
                    f"健康检查失败：{exc}",
                )

        # 直接跑一次脚本：证明「脚本正文 → studio → 子进程 → stdout」这条链是通的。
        script_ok = False
        script_detail = ""
        try:
            async with SessionLocal() as sdb:
                ok, text = await exec_skill_script(
                    sdb,
                    skill=SKILL_CODE,
                    script=SCRIPT_PATH,
                    argv=None,
                    stdin="",
                    timeout_ms=None,
                    allowed_codes={SKILL_CODE},
                )
            script_ok = bool(ok) and SCRIPT_MARK in text
            script_detail = text.strip().replace("\n", " ")[:160]
        except Exception as exc:  # noqa: BLE001
            script_detail = f"执行失败：{exc}"
        report.add(
            "E2E-22",
            "技能脚本经 studio 真实执行",
            script_ok,
            script_detail,
        )

        # 模型在 run 里真的调了 skill_exec（工具链事件里能看到）。
        skill_exec_calls = [
            name for name in called if name == "skill_exec"
        ]
        report.add(
            "E2E-23",
            "模型在同一次 run 里真实调用了 skill_exec",
            bool(skill_exec_calls),
            f"调用列表={called}",
        )

        # 脚本输出真的回灌进了模型上下文（标记要出现在工具结果里）。
        exec_summary = " ".join(
            str(d.get("summary") or "")
            for d in results
            if str(d.get("tool") or "") == "skill_exec"
        )
        report.add(
            "E2E-24",
            "studio 脚本输出已回灌到模型上下文",
            SCRIPT_MARK in exec_summary,
            f"回灌片段={exec_summary[:120]}",
        )

        # 审计里也要有 skill_exec 这一条。
        exec_audits = [
            a for a in audit_rows if str(a.get("tool_name") or "") == "skill_exec"
        ]
        report.add(
            "E2E-25",
            "skill_exec 调用审计已落库",
            bool(exec_audits),
            f"审计条数={len(audit_rows)}，命中 skill_exec={len(exec_audits)}",
        )

        if not args.keep:
            await cleanup(conv_id)

    return 0


async def _close_mcp() -> None:
    """关掉本进程起过的 MCP 连接。"""
    try:
        await mcp_manager.close_all()
    except Exception:  # noqa: BLE001
        pass


async def _close_studio() -> None:
    """关掉本进程起过的 studio 客户端。

    脚本为了直连 studio 自己配了一个客户端（见 E2E-21）；进程退出前要
    把它关掉，否则 gRPC 的 aio 通道会留个未关闭的告警。
    """
    try:
        from app.core.studio import close_studio_client

        await close_studio_client()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    """解析参数并跑流程。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="端到端验收流程")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--llm-code", default="minimax")
    parser.add_argument("--oidp-code", default="local-idp")
    parser.add_argument("--skip-oidc", action="store_true", help="不同步 OIDC 供应商")
    parser.add_argument(
        "--mcp-script",
        default="/app/scripts/e2e_mcp_server.py",
        help="MCP 服务器脚本在容器内的绝对路径",
    )
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--keep", action="store_true", help="跑完保留布场数据")
    parser.add_argument("--json", action="store_true", help="额外打印一行 JSON 报告")
    args = parser.parse_args()

    report = Report()
    try:
        code = asyncio.run(run_flow(args, report))
    except Exception as exc:  # noqa: BLE001
        report.add("FLOW", "流程执行", False, f"{type(exc).__name__}: {exc}")
        print(report.render())
        return 2
    finally:
        asyncio.run(_close_mcp())
        asyncio.run(_close_studio())

    print(report.render())
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    if report.failed:
        return 1
    return code






# ---------------------------------------------------------------- 登录供应商


async def _admin_db_fetch_oidp(code: str) -> dict[str, Any] | None:
    """从管理端库读一条 OIDC 供应商配置（含客户端密钥密文）。

    Args:
        code: 供应商 code。

    Returns:
        配置字典，读不到时返回 ``None``。
    """
    url = (settings.admin_database_url or "").strip()
    if not url:
        return None
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.text(
                        "select code, display_name, kind, issuer, discovery_url, "
                        "authorization_endpoint, token_endpoint, jwks_uri, "
                        "client_id, client_secret_cipher, token_auth_method, "
                        "scopes, username_claim, role_claim, role_claim_required, "
                        "role_sync_max_age_seconds, auto_create, "
                        "client_login_enabled, admin_login_enabled, enabled "
                        "from oauth_provider where code = :code"
                    ),
                    {"code": code},
                )
            ).mappings().first()
        if row is None:
            return None
        return dict(row)
    finally:
        await engine.dispose()


async def sync_oidc_provider(
    db: AsyncSession, code: str
) -> tuple[dict[str, Any] | None, str]:
    """把管理端配置好的 OIDC 供应商镜像到运行时库。

    **为什么需要**：客户端控制台要能登录，就得有��个**指得着的**登录供应商。
    管理端与运行时分库，管理端配好的供应商在运行时库里并不存在——照抄一份
    过去，浏览器里的「登录」按钮才有意义（也是本流程能在浏览器里复现的前提）。

    Args:
        db: 运行时库会话。
        code: 供应商 code。

    Returns:
        ``(配置, 来源说明)``；没有可同步的来源时配置为 ``None``。
    """
    from app.models.oauth import OAuthProvider

    payload = await _admin_db_fetch_oidp(code)
    if not payload:
        return None, "none"
    row = await db.scalar(
        sa.select(OAuthProvider).where(
            OAuthProvider.tenant_id == "default",
            OAuthProvider.code == code,
        )
    )
    fields = {
        "display_name": payload["display_name"],
        "kind": payload["kind"],
        "issuer": payload["issuer"],
        "discovery_url": payload["discovery_url"],
        "authorization_endpoint": payload["authorization_endpoint"],
        "token_endpoint": payload["token_endpoint"],
        "jwks_uri": payload["jwks_uri"],
        "client_id": payload["client_id"],
        "client_secret_cipher": payload["client_secret_cipher"],
        "token_auth_method": payload["token_auth_method"],
        # 最小 IdP 只支持 openid/profile/email；照抄带 phone 的 scope 会让
        # 授权请求被拒，所以这里收窄到 IdP 实际声明的范围。
        "scopes": "openid profile email",
        "username_claim": payload["username_claim"],
        "role_claim": payload["role_claim"],
        "role_claim_required": payload["role_claim_required"],
        "role_sync_max_age_seconds": payload["role_sync_max_age_seconds"],
        "auto_create": 1,
        "client_login_enabled": 1,
        "admin_login_enabled": 0,
        "enabled": 1,
    }
    if row is None:
        row = OAuthProvider(
            tenant_id="default", code=code, **fields
        )
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return {
        "code": row.code,
        "issuer": row.issuer,
        "client_id": row.client_id,
        "scopes": row.scopes,
    }, "admin-db"

if __name__ == "__main__":
    raise SystemExit(main())



