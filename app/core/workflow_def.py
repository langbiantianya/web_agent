"""Workflow definition 解析与校验（设计文档 §4.2 / §6.1）。

V1 只支持线性流水线与单节点，但 ``edges`` 字段保留，为 V2 的 DAG / 条件路由
预留扩展位；``linear_order()`` 以拓扑排序求执行顺序，入度 > 1 时说明是 DAG，
V1 会明确拒绝而不是静默按错误顺序执行。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    agentCode: str = Field(min_length=1, max_length=64)
    outputKey: str = Field(default="", max_length=64)


class WorkflowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    entry: str = Field(default="", description="入口节点 id；为空时取第一个节点")
    nodes: list[WorkflowNode] = Field(min_length=1)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    inputTemplate: str = Field(
        default="用户问题：{{user_input}}\n上游产出：{{upstream}}"
    )

    @field_validator("nodes")
    @classmethod
    def _unique_node_ids(cls, v: list[WorkflowNode]) -> list[WorkflowNode]:
        """校验节点 id 不重复。

        id 是 ``upstream`` 键、事件里的 ``node`` 字段和 step 记录的外键，重复
        会让同一份上游产出被两个节点静默覆盖，属于配置期就该拦下的错。

        Raises:
            ValueError: 存在重复 id。由 pydantic 包装为 ValidationError 上抛。
        """
        ids = [n.id for n in v]
        if len(ids) != len(set(ids)):
            raise ValueError("节点 id 重复")
        return v

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        """model 级校验：一次把「图结构是否 V1 支持」判干净。

        依次检查三件事——edge 端点存在、entry 节点存在、拓扑排序能覆盖全部节点
        （无环）。最后再要求「边数 == 节点数 - 1」，把分叉/DAG 挡在门外。

        全部在配置期失败而不是执行期：definition 由用户手写，跑一半才发现编排
        不了，前面的 token、钱和时间都已经花出去了。

        Raises:
            ValueError: edge 指向不存在的节点、entry 不存在、存在环，或含分叉
                （DAG）。V1 只支持线性流水线。
        """
        ids = {n.id for n in self.nodes}
        for e in self.edges:
            if e.from_ not in ids or e.to not in ids:
                raise ValueError(f"edge 引用了不存在的节点：{e.from_} -> {e.to}")
        if self.entry and self.entry not in ids:
            raise ValueError(f"entry 节点不存在：{self.entry}")
        # 线性流水线无环：沿边走一遍必须能覆盖全部节点
        order = self._topo_order()
        if len(order) != len(ids):
            raise ValueError("definition 存在环，V1 仅支持线性流水线")
        # 边数 == 节点数 - 1 才是一棵链；多出来的边意味着分叉，属于 V2 的 DAG 能力。
        # 在配置期就拒绝，好过跑到一半才发现编排不了。
        if len(self.edges) != len(ids) - 1:
            raise ValueError("definition 含分叉（DAG），V1 仅支持线性流水线")
        return self

    @property
    def entry_id(self) -> str:
        """入口节点 id：显式 ``entry`` 优先，否则取 ``nodes`` 的第一个。

        用 ``nodes[0]`` 而不是拓扑序的起点，是为了让「不写 entry 的单节点 /
        单节点流水线」保持定义即执行的直觉顺序。
        """
        return self.entry or self.nodes[0].id

    def node_by_id(self, node_id: str) -> WorkflowNode:
        """按 id 找节点。

        线性流程节点数很少，顺序遍历比建索引更简单，也不必担心索引与 nodes 的
        同步。

        Raises:
            KeyError: id 不存在。调用方是已经过 ``_validate_graph`` 校验的内部
                路径，理论上不可达。
        """
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def _topo_order(self) -> list[WorkflowNode]:
        """Kahn 拓扑排序，返回节点执行顺序。

        同层按 ``nodes`` 里的**声明顺序**排（``declared`` 映射），而不是按 id
        或 set 的迭代顺序——前者让「配置里怎么写就怎么执行」可预期、可复现，
        后者随 hash 随机化漂移。

        V1 已在校验期拒绝分叉，所以这里的入度最大为 1；保留通用 Kahn 实现是
        为 V2 的 DAG 留出扩展位，届时无需重写。
        """
        ids = {n.id for n in self.nodes}
        indeg: dict[str, int] = {i: 0 for i in ids}
        adj: dict[str, list[str]] = {i: [] for i in ids}
        for e in self.edges:
            adj[e.from_].append(e.to)
            indeg[e.to] += 1

        # Kahn 算法；同层按 definition 中的声明顺序，保证执行顺序稳定可预期
        declared = {n.id: i for i, n in enumerate(self.nodes)}
        ready = sorted((i for i, d in indeg.items() if d == 0), key=declared.get)
        out: list[WorkflowNode] = []
        while ready:
            cur = ready.pop(0)
            out.append(self.node_by_id(cur))
            for nxt in adj[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
                    ready.sort(key=declared.get)
        return out

    def linear_order(self) -> list[WorkflowNode]:
        """返回执行顺序。分叉（入度 > 1）视为 DAG，V1 不支持。"""
        order = self._topo_order()
        if len(self.edges) != len(self.nodes) - 1:
            raise ValueError("definition 含分叉（DAG），V1 仅支持线性流水线")
        return order

    def template_vars(self) -> set[str]:
        """收集 ``inputTemplate`` 里用到的全部 ``{{变量}}``。

        供保存工作流时做静态检查：``user_input`` / ``upstream`` 是保留变量，其余
        名字必须能在上游节点的 outputKey 里找到，否则模板会原样输出 ``{{xxx}}``
        给模型看。
        """
        return set(_VAR_RE.findall(self.inputTemplate))

    def render_input(self, user_input: str, upstream: dict[str, Any]) -> str:
        """渲染 inputTemplate。

        ``{{upstream}}`` 整体替换为上游节点产出摘要；``{{outputKey}}``
        可单独引用某个上游节点，便于精确传递。
        """
        upstream_text = _render_upstream(upstream)
        return self.inputTemplate.replace("{{user_input}}", user_input).replace(
            "{{upstream}}", upstream_text
        )


def _render_upstream(upstream: dict[str, Any]) -> str:
    """把上游产出拼成给模型看的一段 Markdown 摘要。

    上游为空时返回「（无）」而不是空串——空模板会渲染成 ``上游产出：`` 后面紧跟
    下一行，模型容易误判成格式错乱，反而影响输出。

    按标题分块（``## key``）而非裸拼接：多个上游节点产出混在一起时，模型很难
    分清哪段是谁说的。
    """
    if not upstream:
        return "（无）"
    lines = [f"## {key}\n{value}" for key, value in upstream.items()]
    return "\n\n".join(lines)


def render_template(template: str, variables: dict[str, Any]) -> str:
    """把 ``{{ name }}`` 替换为变量值，未知变量保持原样。"""
    def _sub(m: re.Match[str]) -> str:
        """``re.sub`` 的替换回调：命中一个 ``{{ name }}``。

        未命中的变量名**原样返回**而不是替换成空串——拼错的名字要能被肉眼发现，
        悄悄抹掉会变成一个少了一句话的 prompt，比报错更难查。
        """
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        return m.group(0)

    return _VAR_RE.sub(_sub, template)
