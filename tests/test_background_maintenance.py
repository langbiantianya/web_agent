"""后台维护任务：MCP 空闲回收、checkpoint 孤儿清理与提示词边界。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import settings
from app.core import checkpoint as checkpoint_mod
from app.core.mcp.manager import McpClientManager
from app.core.tools import OPTIONAL_TOOLS
from app.models.enums import RunStatus
from app.models.run import Run


class _FakeConnection:
    """只暴露空闲回收所需的连接字段。"""

    def __init__(self, last_used: float, inflight: int = 0) -> None:
        """设置最后使用时间与在途请求数。"""
        self.code = "fake"
        self._last_used = last_used
        self._inflight = inflight
        self.closed = False

    async def close(self) -> None:
        """记录连接已关闭。"""
        self.closed = True


@pytest.mark.asyncio
async def test_mcp_idle_reaper_closes_idle_but_not_inflight(monkeypatch):
    """空闲连接会被回收，有在途请求的连接必须保留。"""
    manager = McpClientManager()
    idle = _FakeConnection(last_used=0)
    busy = _FakeConnection(last_used=0, inflight=1)
    manager._connections = {"idle": idle, "busy": busy}  # noqa: SLF001
    monkeypatch.setattr(settings, "mcp_idle_timeout_sec", 10)

    assert await manager.close_idle() == 1
    assert idle.closed is True
    assert busy.closed is False
    assert set(manager._connections) == {"busy"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_mcp_idle_reaper_task_can_start_and_stop(monkeypatch):
    """周期任务可重复启动，且停止时不会留下后台任务。"""
    manager = McpClientManager()
    monkeypatch.setattr(settings, "mcp_idle_sweep_interval_sec", 0.01)

    manager.start_idle_reaper()
    first = manager._sweeper  # noqa: SLF001
    manager.start_idle_reaper()
    assert manager._sweeper is first  # noqa: SLF001

    await manager.stop_idle_reaper()
    assert first is not None and first.cancelled()
    assert manager._sweeper is None  # noqa: SLF001


class _CheckpointTuple:
    """checkpoint ``alist`` 返回项的最小替身。"""

    def __init__(self, thread_id: str, when: datetime) -> None:
        """保存 thread id 与 checkpoint 时间。"""
        self.config = {"configurable": {"thread_id": thread_id}}
        self.checkpoint = {"ts": when.isoformat()}


class _FakeSaver:
    """支持枚举与删除 thread 的 checkpointer 替身。"""

    def __init__(self, items) -> None:
        """保存扫描项。"""
        self.items = items
        self.deleted: set[str] = set()

    async def alist(self, _config, **_kwargs):
        """逐条产出扫描项。"""
        for item in self.items:
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        """记录删除的 thread。"""
        self.deleted.add(thread_id)


@pytest.mark.asyncio
async def test_checkpoint_cleanup_removes_terminal_and_old_orphans(db, monkeypatch):
    """终态 run 立即清理；无主 thread 仅在超过保留期后清理。"""
    terminal = Run(
        conversation_id=900001,
        workflow_id=900001,
        status=RunStatus.ERROR.value,
        last_seq=0,
    )
    waiting = Run(
        conversation_id=900002,
        workflow_id=900002,
        status=RunStatus.WAITING.value,
        last_seq=0,
    )
    db.add_all([terminal, waiting])
    await db.commit()
    await db.refresh(terminal)
    await db.refresh(waiting)

    old = datetime.now(UTC) - timedelta(hours=2)
    fresh = datetime.now(UTC)
    items = [
        _CheckpointTuple(f"run-{terminal.id}", old),
        _CheckpointTuple(f"run-{waiting.id}", old),
        _CheckpointTuple("run-999999901", old),
        _CheckpointTuple("run-999999902", fresh),
        _CheckpointTuple("foreign-thread", old),
    ]
    saver = _FakeSaver(items)
    monkeypatch.setattr(checkpoint_mod, "_saver", saver)
    monkeypatch.setattr(settings, "checkpoint_orphan_retention_sec", 3600)

    try:
        deleted = await checkpoint_mod.cleanup_orphan_checkpoints()
        assert deleted == 2
        assert saver.deleted == {
            f"run-{terminal.id}",
            "run-999999901",
        }
    finally:
        await db.delete(terminal)
        await db.delete(waiting)
        await db.commit()


def test_skill_exec_describes_isolated_startup_latency():
    """工具描述要提醒模型：强隔离容器可能多耗数秒。"""
    description = OPTIONAL_TOOLS["skill_exec"].description

    assert "isolated" in description
    assert "数秒" in description
    assert "内置工具" in description


def test_conftest_disables_grpc_fork_support():
    """测试进程必须在 gRPC 初始化前关闭 fork 支持。"""
    assert os.environ.get("GRPC_ENABLE_FORK_SUPPORT") == "0"
