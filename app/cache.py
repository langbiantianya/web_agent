"""Agent / Workflow 内存缓存。

设计原则「配置即数据」：缓存只是加速手段，MySQL 永远是事实来源。
用 ``(code, version)`` 作为缓存键——更新接口让 ``version+1``，旧键自然失效，
不存在「更新了但缓存没清」这种一致性漏洞；进程重启后缓存自动重建。
"""

from __future__ import annotations

from collections import OrderedDict

from app.models.agent import Agent
from app.models.workflow import Workflow


class _LRU(OrderedDict):
    def __init__(self, maxsize: int) -> None:
        """绑定 LRU 容量上限。

        Args:
            maxsize: 最多保留多少条目，超出后按最久未访问淘汰。
        """
        super().__init__()
        self.maxsize = maxsize

    def put(self, key, value) -> None:
        """写入并顺带维护访问顺序，超出 ``maxsize`` 时淘汰最旧的键。"""
        if key in self:
            self.move_to_end(key)
        self[key] = value
        while len(self) > self.maxsize:
            self.popitem(last=False)


class AgentCache:
    def __init__(self, maxsize: int = 256) -> None:
        """建立空的 LRU 存储（缓存状态全部在进程内存里，随进程生灭）。"""
        self._store: _LRU = _LRU(maxsize)

    @staticmethod
    def _key(code: str, version: int) -> tuple[str, int]:
        """缓存键 = (code, version)。

        Agent 配置更新会让 ``version+1``，于是旧键再也命中不了，
        不需要「更新时记得删缓存」——这正是避免脏读的关键。
        """
        return (code, version)

    def get(self, code: str, version: int) -> Agent | None:
        """按 (code, version) 取缓存；version 对不上即视为未命中，应回源查库。"""
        return self._store.get(self._key(code, version))

    def put(self, agent: Agent) -> None:
        """按实体当前的 code/version 写入缓存（写入方需保证 version 已是最新）。"""
        self._store.put(self._key(agent.code, agent.version), agent)

    def clear(self) -> None:
        """清空缓存（删除 Agent 等无法用 version 递增表达失效的操作后调用）。"""
        self._store.clear()


class WorkflowCache:
    def __init__(self, maxsize: int = 256) -> None:
        """建立空的 LRU 存储（缓存状态全部在进程内存里，随进程生灭）。"""
        self._store: _LRU = _LRU(maxsize)

    @staticmethod
    def _key(code: str, version: int) -> tuple[str, int]:
        """缓存键 = (code, version)，与 :class:`AgentCache` 完全同构。

        V3 之前 workflow 表没有 version 列，只能用 (code, "") 这样的假键，
        于是「更新后记得调用方 ``put()``」成了唯一的一致性保证——一个纯粹靠
        纪律维持的约定，任何一条写漏的路径都会读到旧定义。加了 version 列之后
        键能带上它：更新让 version+1，旧键自然命中不了，调用方漏调 ``put()``
        也只是退化成回源查库，不会读到脏数据。
        """
        return (code, version)

    def get(self, code: str, version: int) -> Workflow | None:
        """按 (code, version) 取缓存；version 对不上即视为未命中，应回源查库。"""
        return self._store.get(self._key(code, version))

    def put(self, wf: Workflow) -> None:
        """按实体当前的 code/version 写入缓存。

        version 已是最新；写入方若刚改了 definition，需要重新 put 才会命中新键
        （但漏了也只是回源，不会脏读）。
        """
        self._store.put(self._key(wf.code, wf.version), wf)

    def clear(self) -> None:
        """清空缓存（删除 Workflow 等无法用 version 递增表达失效的操作后调用）。"""
        self._store.clear()


agent_cache = AgentCache()
workflow_cache = WorkflowCache()
