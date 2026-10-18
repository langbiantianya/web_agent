"""pytest 全局配置。

环境变量必须在 import app.* 之前设定——app.config.settings 在导入期就会读取
DATABASE_URL 并据此创建引擎。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --- 测试环境配置（先于任何 app.* 导入）---
# 库文件名带上进程号：早先固定名 %TEMP%\web_agent_test.db 时，并行跑两遍 pytest
# 会互相覆盖同一个文件（还会在 import 期 unlink 掉对方正在用的库，Windows 上直接
# 抛 PermissionError），表现为毫无关联的用例随机失败。按进程隔离即可各跑各的。
_TMP_DB = Path(tempfile.gettempdir()) / f"web_agent_test_{os.getpid()}.db"
if _TMP_DB.exists():
    try:
        _TMP_DB.unlink()
    except OSError:  # pragma: no cover - 上一次进程残留且被占用
        pass

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789"
os.environ["DEBUG"] = "false"
# 让批量落库在测试里几乎立即发生，但不关掉批量逻辑本身
os.environ["EVENT_BATCH_SIZE"] = "5"
os.environ["EVENT_FLUSH_INTERVAL"] = "0.02"
os.environ["NODE_RETRY_BASE_DELAY"] = "0.01"

from app.core.run_hub import get_hub  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db_schema():
    """建表（session 级）。

    刻意做成**同步** fixture：pytest-asyncio 下 session 级 async fixture 会绑定
    到一个独立的 loop，与用例所在 loop 冲突。SQLite 走 NullPool，每次新开连接，
    因此这里建表、那边读写不会串。
    """
    import asyncio

    from app.config.db import engine

    async def _create():
        """建表动作本体；放进 ``engine.begin()`` 让任一张表失败即整批回滚。"""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())


@pytest.fixture(scope="session", autouse=True)
def _cleanup_db_file():
    """会话结束后删掉本进程的测试库，不在系统临时目录留垃圾。"""
    yield
    try:
        _TMP_DB.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 仍有连接未释放
        pass


@pytest.fixture(autouse=True)
async def _clean_hub():
    """每个用例前后清空 RunHub 状态，避免 seq 计数器与后台任务串味。"""
    yield
    hub = get_hub()
    for run_id in list(hub._states):  # noqa: SLF001
        try:
            await hub.finalize(run_id)
        except Exception:
            pass


@pytest.fixture
async def db():
    """请求外的独立 AsyncSession，用于直接断言 DB 事实。"""
    from app.config.db import SessionLocal

    async with SessionLocal() as session:
        yield session
