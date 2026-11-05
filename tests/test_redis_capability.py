"""Redis 事件能力与 RediSearch 审批能力探测。"""

from __future__ import annotations

import pytest
from redis.exceptions import ResponseError

from app.core.redis_client import redis_search_available


class _Client:
    """只实现能力探测所需命令的假客户端。"""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        """保存预设命令元数据或异常。"""
        self.result = result
        self.error = error

    async def execute_command(self, *args):
        """记录命令并返回预设结果。"""
        assert args[:2] == ("COMMAND", "INFO")
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_redis_search_probe_accepts_known_command():
    """命令元数据存在表示 RediSearch 已加载。"""
    client = _Client(
        [
            {
                "name": "ft.create",
                "arity": -2,
            }
        ]
    )
    assert await redis_search_available(client) is True


@pytest.mark.asyncio
async def test_redis_search_probe_rejects_unknown_or_denied_command():
    """普通 Redis 与 ACL 拒绝都返回 False，启动层可给出可操作提示。"""
    assert await redis_search_available(_Client([None])) is False
    assert (
        await redis_search_available(
            _Client(error=ResponseError("unknown command"))
        )
        is False
    )
