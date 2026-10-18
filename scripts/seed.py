"""初始化演示数据：3 个 Agent + 1 个三节点 Workflow。

用法：
    python -m scripts.seed

演示用的模型从环境变量读取（见 .env）：

    MINIMAX_BASE_URL   默认 https://api.minimaxi.com/v1（OpenAI 兼容协议）
    MINIMAX_MODEL      默认 MiniMax-M2.7
    MINIMAX_API_KEY    必填，落在 .env 里，不要提交

换任何 OpenAI 兼容服务（DeepSeek / Qwen / vLLM / OpenAI）都只需改这三个值，
**不需要改代码**——这正是「配置即数据」的意义。
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import inspect as sa_inspect

from app.config.db import SessionLocal, engine
from app.config.settings import settings
from app.core.crypto import encrypt
from app.repo.agent_repo import agent_repo
from app.repo.workflow_repo import workflow_repo

DEMO_BASE_URL = settings.minimax_base_url
DEMO_MODEL = settings.minimax_model
DEMO_API_KEY = settings.minimax_api_key

AGENTS = [
    {
        "code": "planner",
        "name": "规划 Agent",
        "description": "拆解用户问题，产出执行计划",
        "system_prompt": (
            "你是一名任务规划专家。把用户的问题拆解成 3-5 条可执行的步骤，"
            "只输出计划本身，不要寒暄。"
        ),
        "model": DEMO_MODEL,
        "base_url": DEMO_BASE_URL,
        "temperature": 0.3,
        "max_tokens": 1024,
    },
    {
        "code": "researcher",
        "name": "研究 Agent",
        "description": "根据计划补充事实与要点",
        "system_prompt": (
            "你是一名研究员。基于给定计划逐条展开，补充关键事实与注意点，"
            "用中文条目输出。"
        ),
        "model": DEMO_MODEL,
        "base_url": DEMO_BASE_URL,
        "temperature": 0.5,
        "max_tokens": 1536,
    },
    {
        "code": "writer",
        "name": "写作 Agent",
        "description": "把上游产出整合成最终答复",
        "system_prompt": (
            "你是一名撰稿人。把计划与研究要点整合成结构清晰、语言流畅的最终答复。"
        ),
        "model": DEMO_MODEL,
        "base_url": DEMO_BASE_URL,
        "temperature": 0.7,
        "max_tokens": 2048,
    },
]

WORKFLOW = {
    "code": "default_pipeline",
    "name": "默认三段式流水线",
    "description": "planner → researcher → writer 的线性流水线",
    "definition": {
        "entry": "planner",
        "nodes": [
            {"id": "planner", "agentCode": "planner", "outputKey": "plan"},
            {"id": "researcher", "agentCode": "researcher", "outputKey": "facts"},
            {"id": "writer", "agentCode": "writer", "outputKey": "answer"},
        ],
        "edges": [
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "writer"},
        ],
        "inputTemplate": "用户问题：{{user_input}}\n\n上游产出：\n{{upstream}}",
    },
}


async def _schema_ready() -> bool:
    """探测数据库里是否已经存在业务表。

    这里刻意**不**用 ``Base.metadata.create_all`` 建表。那样表是绕过 Alembic
    凭空出现的，``alembic_version`` 仍然是空的；之后再跑 ``alembic upgrade head``
    就会因 "table agent already exists" 直接失败，把人卡在建表这一步。
    建表是 Alembic 的职责，seed 只负责灌演示数据。

    以 ``agent`` 表为探针：它是最早建的表之一，存在即代表迁移已执行过。
    """
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync_conn: sa_inspect(sync_conn).has_table("agent"))


async def main() -> int:
    """初始化演示数据并返回退出码。

    依次做三件事：确认表结构已由迁移建好（没有就提示先跑 ``alembic upgrade head``，
    而不是自己建表）→ 写入 3 个演示 Agent（planner / researcher / writer，api_key
    加密后落库）→ 写入 1 个串起三者的线性 Workflow ``default_pipeline``。
    已存在的按 code 跳过，因此可以重复执行。

    模型与端点来自环境变量（``MINIMAX_BASE_URL`` / ``MINIMAX_MODEL`` /
    ``MINIMAX_API_KEY``，默认见 .env），换任何 OpenAI 兼容服务都只改配置，
    不需要改这里的代码；缺 API Key 只告警不失败，方便接本地 vLLM。

    返回码：0 成功；1 表结构不存在（需要先迁移）。
    """
    if not await _schema_ready():
        print("❌ 数据库里还没有业务表，请先建表：")
        print("   python -m alembic upgrade head")
        return 1
    print("✅ 表结构已就绪")

    if not DEMO_API_KEY:
        print("⚠️  未配置 MINIMAX_API_KEY，Agent 的 api_key 将为空。")
        print("    本地 vLLM 等无需鉴权的端点可以忽略；否则请在 .env 里设置。")

    print(f"ℹ️  演示模型：{DEMO_MODEL} @ {DEMO_BASE_URL}\n")

    async with SessionLocal() as db:
        for spec in AGENTS:
            payload = {**spec, "api_key_cipher": encrypt(DEMO_API_KEY), "enabled": 1}
            if await agent_repo.get_by_code(db, spec["code"]):
                print(f"· Agent {spec['code']} 已存在，跳过")
                continue
            await agent_repo.create(db, **payload)
            print(f"✅ Agent {spec['code']} 已创建")

        if await workflow_repo.get_by_code(db, WORKFLOW["code"]):
            print("· Workflow default_pipeline 已存在，跳过")
        else:
            await workflow_repo.create(
                db, **{**WORKFLOW, "enabled": 1}
            )
            print("✅ Workflow default_pipeline 已创建")

    print("\n下一步：uvicorn app.main:app --reload")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
