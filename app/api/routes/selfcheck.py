"""运行时链路自检：把「模型端点 → 智能体 → 技能 → MCP → 工作流」摊开。

客户端控制台以前只能看到 Agent 与工作流两个列表（``/api/catalog``），技能、
MCP 服务器与 LLM 端点这几段链路是**看不见**的——于是「为什么这个 Agent 不
调工具」「为什么工作流卡住」这类问题只能靠猜。

这个接口把链路的每一段都列出来，并给出**逐段的通过/失败判定**：

- ``llm`` —— Agent 绑定的端点存在、启用、有密钥、模型名非空；
- ``skill`` —— 绑定的技能存在、启用、已发布；
- ``mcp`` —— 绑定的 MCP 工具在工具快照里，且它的服务器是启用的；
- ``graph`` —— 工作流图结构合法（复用与保存期同一份规则）。

**只读**，且只回显「够判断问题」的信息：端点地址只给主机名（不给路径与
凭据），技能与工具只给 code 与名字，不回显正文与参数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter

from app.api.deps import CurrentUserDep, DbSession
from app.core.mcp.naming import is_mcp_name
from app.core.workflow_def import collect_graph_issues
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.skill_repo import skill_repo
from app.repo.workflow_repo import workflow_repo

router = APIRouter(prefix="/api/selfcheck", tags=["selfcheck"])

PASS = "pass"
FAIL = "fail"
WARN = "warn"


def _host(base_url: str) -> str:
    """只取地址的主机名。

    端点地址是部署信息：路径里可能有租户标识，凭据可能写在 userinfo 里。
    判断「配的是哪个服务商」只需要主机名。

    Args:
        base_url: 端点地址。

    Returns:
        主机名；解析不出来时原样返回（由前端显示）。
    """
    try:
        parsed = urlsplit(base_url or "")
    except ValueError:
        return base_url or ""
    return parsed.hostname or base_url or ""


def _stage(key: str, label: str, status: str, detail: str) -> dict[str, Any]:
    """构造一个阶段判定。

    Args:
        key: 阶段标识。
        label: 阶段名。
        status: 通过/失败/提醒。
        detail: 说明。

    Returns:
        阶段字典。
    """
    return {"key": key, "label": label, "status": status, "detail": detail}


async def _selfcheck_payload(db, tenant_id: str) -> dict[str, Any]:
    """汇总链路自检数据。

    Args:
        db: 数据库会话。
        tenant_id: 租户。

    Returns:
        自检报告。
    """
    llm_rows = await llm_endpoint_repo.list(db, limit=500)
    agent_rows = await agent_repo.list(db, limit=500, tenant_id=tenant_id)
    skill_rows = await skill_repo.list(db, limit=500, tenant_id=tenant_id)
    server_rows = await mcp_server_repo.list(db, limit=500, tenant_id=tenant_id)
    workflow_rows = await workflow_repo.list(db, limit=500)

    llm_by_code = {row.code: row for row in llm_rows}
    skill_by_code = {row.code: row for row in skill_rows}
    server_by_code = {row.code: row for row in server_rows}
    agent_by_code = {row.code: row for row in agent_rows}

    # MCP 工具快照：限定名 → 工具行。限定名本身带服务器 code，能定位到服务器。
    tool_names: set[str] = set()
    tool_owner: dict[str, str] = {}
    for server in server_rows:
        for tool in await mcp_tool_repo.list_by_server(db, server.id):
            tool_names.add(tool.qualified_name)
            tool_owner[tool.qualified_name] = server.code

    llm_out = [
        {
            "code": row.code,
            "name": row.name,
            "host": _host(row.base_url),
            "model": row.model,
            "priority": row.priority,
            "enabled": bool(row.enabled),
            "key_configured": bool(row.api_key_cipher),
        }
        for row in llm_rows
    ]

    agent_out = []
    for row in agent_rows:
        endpoint = llm_by_code.get(row.llm_endpoint_code or "")
        agent_out.append(
            {
                "code": row.code,
                "name": row.name,
                "model": row.model,
                "llm_endpoint_code": row.llm_endpoint_code,
                "llm_ready": bool(
                    endpoint is not None
                    and endpoint.enabled
                    and endpoint.api_key_cipher
                    and endpoint.model
                ),
                "skills": list(row.skills or []),
                "tools": list(row.tools or []),
                "enabled": bool(row.enabled),
            }
        )

    skill_out = [
        {
            "code": row.code,
            "name": row.name,
            "version": row.version,
            "status": row.status,
            "enabled": bool(row.enabled),
            "chars": len(row.body or ""),
        }
        for row in skill_rows
    ]

    mcp_out = []
    for server in server_rows:
        tools = await mcp_tool_repo.list_by_server(db, server.id)
        mcp_out.append(
            {
                "code": server.code,
                "name": server.name,
                "transport": server.transport,
                "enabled": bool(server.enabled),
                "tool_count": len(tools),
                "tools": [tool.qualified_name for tool in tools],
            }
        )

    chains: list[dict[str, Any]] = []
    for wf in workflow_rows:
        definition = wf.definition or {}
        stages: list[dict[str, Any]] = []
        issues = collect_graph_issues(definition)
        stages.append(
            _stage(
                "graph",
                "工作流图结构",
                PASS if not issues else FAIL,
                "结构合法" if not issues else "；".join(i.message for i in issues[:3]),
            )
        )

        for node in definition.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            node_type = str(node.get("type") or "agent")
            if node_type == "approval":
                stages.append(
                    _stage(
                        f"node:{node_id}",
                        f"节点 {node_id}",
                        PASS if node.get("prompt") else FAIL,
                        "人工审批节点，提示语已配置"
                        if node.get("prompt")
                        else "审批节点缺少提示语",
                    )
                )
                continue
            agent_code = str(node.get("agentCode") or "")
            agent = agent_by_code.get(agent_code)
            if agent is None:
                stages.append(
                    _stage(
                        f"node:{node_id}",
                        f"节点 {node_id}",
                        FAIL,
                        f"引用的智能体「{agent_code}」不存在",
                    )
                )
                continue
            endpoint = llm_by_code.get(agent.llm_endpoint_code or "")
            llm_ok = bool(
                endpoint is not None
                and endpoint.enabled
                and endpoint.api_key_cipher
                and endpoint.model
            )
            stages.append(
                _stage(
                    f"node:{node_id}:llm",
                    f"节点 {node_id} · 模型端点",
                    PASS
                    if llm_ok
                    else FAIL,
                    f"{agent.llm_endpoint_code or '未绑定'} / {agent.model}"
                    if llm_ok
                    else "端点缺失、停用、无密钥或未选模型",
                )
            )

            skill_status = PASS
            skill_detail = "未绑定技能"
            if agent.skills:
                missing = [c for c in agent.skills if c not in skill_by_code]
                disabled = [
                    c
                    for c in agent.skills
                    if c in skill_by_code
                    and not (
                        skill_by_code[c].enabled
                        and skill_by_code[c].status == "published"
                    )
                ]
                if missing:
                    skill_status = FAIL
                    skill_detail = f"技能不存在：{'、'.join(missing)}"
                elif disabled:
                    skill_status = WARN
                    skill_detail = f"技能未启用或未发布：{'、'.join(disabled)}"
                else:
                    skill_detail = f"已绑定 {len(agent.skills)} 个技能"
            stages.append(
                _stage(
                    f"node:{node_id}:skill",
                    f"节点 {node_id} · 技能",
                    skill_status,
                    skill_detail,
                )
            )

            mcp_tools = [t for t in (agent.tools or []) if is_mcp_name(t)]
            if not mcp_tools:
                stages.append(
                    _stage(
                        f"node:{node_id}:mcp",
                        f"节点 {node_id} · MCP",
                        WARN,
                        "未绑定 MCP 工具",
                    )
                )
            else:
                unknown = [t for t in mcp_tools if t not in tool_names]
                off_server: list[str] = []
                for tool_name in mcp_tools:
                    if tool_name not in tool_names:
                        continue
                    owner = server_by_code.get(tool_owner.get(tool_name, ""))
                    if owner is None or not owner.enabled:
                        off_server.append(tool_name)
                if unknown:
                    stages.append(
                        _stage(
                            f"node:{node_id}:mcp",
                            f"节点 {node_id} · MCP",
                            FAIL,
                            f"工具未在目录中：{'、'.join(unknown[:3])}",
                        )
                    )
                elif off_server:
                    stages.append(
                        _stage(
                            f"node:{node_id}:mcp",
                            f"节点 {node_id} · MCP",
                            FAIL,
                            f"工具所属服务器已停用：{'、'.join(off_server[:3])}",
                        )
                    )
                else:
                    stages.append(
                        _stage(
                            f"node:{node_id}:mcp",
                            f"节点 {node_id} · MCP",
                            PASS,
                            f"可调用 {len(mcp_tools)} 个工具",
                        )
                    )

        failed = sum(1 for s in stages if s["status"] == FAIL)
        chains.append(
            {
                "workflow_code": wf.code,
                "workflow_name": wf.name,
                "enabled": bool(wf.enabled),
                "stages": stages,
                "passed": sum(1 for s in stages if s["status"] == PASS),
                "failed": failed,
            }
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "tenant_id": tenant_id,
        "llm_endpoints": llm_out,
        "agents": agent_out,
        "skills": skill_out,
        "mcp_servers": mcp_out,
        "workflows": [
            {
                "code": wf.code,
                "name": wf.name,
                "enabled": bool(wf.enabled),
                "nodes": [
                    {
                        "id": n.get("id"),
                        "type": n.get("type") or "agent",
                        "agent_code": n.get("agentCode") or "",
                    }
                    for n in (wf.definition or {}).get("nodes", [])
                    if isinstance(n, dict)
                ],
            }
            for wf in workflow_rows
        ],
        "chains": chains,
        "summary": {
            "llm_endpoints": len(llm_out),
            "agents": len(agent_out),
            "skills": len(skill_out),
            "mcp_servers": len(mcp_out),
            "workflows": len(workflow_rows),
            "failed_stages": sum(c["failed"] for c in chains),
            "ready": all(c["failed"] == 0 for c in chains) and bool(chains),
        },
    }


@router.get("")
async def get_selfcheck(db: DbSession, user: CurrentUserDep) -> dict[str, Any]:
    """返回运行时链路自检报告。

    Returns:
        自检报告（见模块 docstring）。
    """
    return await _selfcheck_payload(db, user.tenant_id)

