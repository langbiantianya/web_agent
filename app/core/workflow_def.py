"""Workflow definition 解析与校验（设计文档 §4.2 / §6.1）。

V3 起支持 **DAG + 条件边**：

- **无 ``when`` 的边**：无条件走。多个出边 = 并行分叉；多个入边 = 汇聚。
- **带 ``when`` 的边**：条件成立才走。表达式文法见 :mod:`app.core.expr`。

校验在**配置期**一次做完，不留到运行期：definition 是用户手写的，跑一半才发现
编排不了，前面的 token、钱和时间都已经花出去了。

刻意拒绝的三种图结构（理由见各自的方法 docstring）：

1. 环；
2. 从 entry 走不到的孤立节点（死配置，且会让图看起来比实际复杂）；
3. **入边里混了条件边的汇聚节点**——LangGraph 会等齐所有入边才触发汇聚节点，
   某个上游被条件跳过就会永远等不到。

V3.1 起节点还有两种类型（见 :class:`NodeType`）：``agent``（默认，调模型）与
``approval``（人工审批，不调模型等人决策）。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core import expr

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class NodeType(StrEnum):
    """节点类型（V3.1 起有两种）。

    - ``AGENT``：调一个 Agent 产出文本。**默认值**，老 definition 不用改。
    - ``APPROVAL``：人工审批节点。不调模型，而是等人给决策；决策意见作为它的
      产出进入 ``upstream``，下游节点照常读。
    """

    AGENT = "agent"
    APPROVAL = "approval"


class WorkflowNode(BaseModel):
    """一个工作流节点。

    ``type`` 决定它到底是「调模型的节点」还是「等人审批的节点」。默认是
    ``agent``，所以 V1/V2/V3 的 definition 一行都不用改。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    type: str = Field(default=NodeType.AGENT.value, max_length=16)
    agentCode: str = Field(default="", max_length=64)
    outputKey: str = Field(default="", max_length=64)
    # 审批节点上给审批人看的话；只有 type=approval 时才有意义
    prompt: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _validate_node_shape(self) -> WorkflowNode:
        """按类型校验字段组合。

        - ``agent`` 节点必须有 ``agentCode``——没有它就没法调模型；
        - ``approval`` 节点必须有 ``prompt``（审批人要看什么？）且**不能**有
          ``agentCode``：两套语义混在一个节点上，出问题时说不清是哪边生效。

        Raises:
            ValueError: 字段组合不合法。由 pydantic 包装为 ValidationError 上抛。
        """
        if self.type not in {t.value for t in NodeType}:
            raise ValueError(
                f"未知的节点类型 {self.type}；可用："
                f"{'、'.join(t.value for t in NodeType)}"
            )
        if self.type == NodeType.AGENT.value:
            if not self.agentCode:
                raise ValueError(f"节点 {self.id} 是 agent 类型，必须指定 agentCode")
            return self
        if not self.prompt:
            raise ValueError(f"节点 {self.id} 是 approval 类型，必须写 prompt")
        if self.agentCode:
            raise ValueError(
                f"节点 {self.id} 是 approval 类型，不能同时指定 agentCode"
            )
        return self

    @property
    def is_approval(self) -> bool:
        """是否人工审批节点。"""
        return self.type == NodeType.APPROVAL.value

    @property
    def key(self) -> str:
        """该节点产出的键——``upstream`` 里用它取值。

        没写 ``outputKey`` 时退回节点 id，与运行期 :attr:`WorkflowNode.output_key`
        的取法保持一致。
        """
        return self.outputKey or self.id


class WorkflowEdge(BaseModel):
    """一条边。``when`` 为空表示无条件边。

    ``from`` 是 Python 关键字，用 ``from_`` 作属性名并靠 alias 接住 JSON 里的
    ``from``——配置是给人写的，不该逼着写 ``from_``。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    when: str = Field(default="", max_length=200)


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
        """model 级校验：一次把「这张图能不能跑」判干净。

        依次检查：

        1. edge 端点存在、entry 节点存在；
        2. 无环（拓扑排序必须覆盖全部节点）；
        3. 从 entry 出发能走到每个节点（无孤立死配置）；
        4. 汇聚节点的入边不许混条件边（LangGraph 会永远等不到）；
        5. 同一节点的出边不许条件与无条件混用；
        6. 条件表达式语法正确、且只引用本图里真实存在的 outputKey；
        7. inputTemplate 里的变量全部可解析（含拒绝 ``{{upstream.x}}`` 这种
           误解写法）。

        全部在配置期失败而不是执行期：definition 由用户手写，跑一半才发现编排
        不了，前面的 token、钱和时间都已经花出去了。

        Raises:
            ValueError: 上述任一条不满足。由 pydantic 包装为 ValidationError。
        """
        ids = {n.id for n in self.nodes}
        for e in self.edges:
            if e.from_ not in ids or e.to not in ids:
                raise ValueError(f"edge 引用了不存在的节点：{e.from_} -> {e.to}")
        if self.entry and self.entry not in ids:
            raise ValueError(f"entry 节点不存在：{self.entry}")

        # 无环：拓扑排序必须能覆盖全部节点
        order = self._topo_order()
        if len(order) != len(ids):
            raise ValueError("definition 存在环，无法确定执行顺序")

        self._check_reachable(ids, order)
        self._check_fanin_shape(ids)
        self._check_outgoing_uniform()
        self._validate_conditions()
        self._validate_template_vars()
        return self

    def _check_reachable(self, ids: set[str], order: list[WorkflowNode]) -> None:
        """每个节点都必须从 entry 走得到。

        孤立节点是最阴险的一种配置错误：保存不报错、图能编译，但那个节点永远
        不会被执行，用户却以为它在流程里。与其在运行日志里让人猜，不如在保存
        时直接指出来。

        Raises:
            ValueError: 存在走不到的节点。
        """
        adj: dict[str, list[str]] = {i: [] for i in ids}
        for e in self.edges:
            adj[e.from_].append(e.to)

        seen = {self.entry_id}
        queue = [self.entry_id]
        while queue:
            cur = queue.pop()
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)

        orphans = ids - seen
        if orphans:
            raise ValueError(
                f"以下节点从 entry 走不到（孤立节点）：{'、'.join(sorted(orphans))}"
            )

    def _check_fanin_shape(self, ids: set[str]) -> None:
        """汇聚节点的入边必须全是无条件边。

        LangGraph 对多入边节点的处理是「等齐所有入边才触发」。若其中一条是
        条件边而这次没命中，汇聚节点就会永远等不到——run 静默地少跑一段，
        最后还以 done 收尾。

        例外是「所有入边都是条件边」：那就得靠运行期再判一次，超出本版范围，
        因此直接拒绝并说明怎么办。
        """
        incoming: dict[str, list[WorkflowEdge]] = {i: [] for i in ids}
        for e in self.edges:
            incoming[e.to].append(e)

        for node_id, edges in incoming.items():
            if len(edges) < 2:
                continue
            conditional = [e for e in edges if e.when]
            if not conditional:
                continue
            if len(conditional) < len(edges):
                bad = "、".join(f"{e.from_} --when--> {node_id}" for e in conditional)
                raise ValueError(
                    f"节点 {node_id} 是汇聚节点（{len(edges)} 条入边），但入边里混了条件边"
                    f"（{bad}）：条件不成立时它会永远等不到。请把入边全部改成无条件边，"
                    f"或让条件节点直接连到 {node_id} 的下游。"
                )
            raise ValueError(
                f"节点 {node_id} 的 {len(edges)} 条入边全是条件边："
                f"本版不支持「多条条件边汇聚到同一节点」，请改成无条件汇聚。"
            )

    def _check_outgoing_uniform(self) -> None:
        """同一个节点的出边不许条件与无条件混用。

        混用时「无条件那部分到底还走不走」要靠读者自己推断，语义不明确；
        LangGraph 虽然能跑，但排查起来很痛苦。明确二选一更好。
        """
        outgoing: dict[str, list[WorkflowEdge]] = {}
        for e in self.edges:
            outgoing.setdefault(e.from_, []).append(e)

        for node_id, edges in outgoing.items():
            conditional = [e for e in edges if e.when]
            if conditional and len(conditional) < len(edges):
                raise ValueError(
                    f"节点 {node_id} 的出边里条件边与无条件边混用："
                    f"请统一成「全无条件（并行）」或「全条件（分支）」。"
                )

    def _validate_conditions(self) -> None:
        """逐条校验 ``when`` 表达式：语法 + 变量引用。

        变量校验是这里最有价值的一条：``upstream.plan`` 里 ``plan`` 拼错、或引用了
        一个已被删掉的节点，表达式照样能通过语法检查，运行期只会安静地求值成
        ``None`` → 条件为假 → **走错分支**。分支选错不会有任何报错，只会在生产里
        表现为「为什么走了 else 分支」，所以这类错误必须挡在保存时。

        语法只认两种写法：``user_input`` 与 ``upstream.<outputKey>``。刻意**不**
        放行裸 ``plan``：求值期的 scope 只有这两个根（见 run_graph 的路由函数），
        裸名会求值成 ``None``；而放行它还会引出「某个节点恰好叫 ``user_input``」
        这类遮蔽问题。与其留一个静默为假的写法，不如保存时直接告诉正确写法。
        """
        keys = {n.outputKey or n.id for n in self.nodes}
        for e in self.edges:
            if not e.when:
                continue
            where = f"边 {e.from_} -> {e.to}"
            try:
                expr.check_syntax(e.when)
            except expr.ExprError as exc:
                raise ValueError(f"{where} 的 when 表达式有语法错误：{exc}") from exc

            for path in expr.variables(e.when):
                if path[0] == "user_input":
                    continue
                if path[0] != "upstream":
                    hint = f"；请写成 upstream.{path[0]}" if path[0] in keys else ""
                    raise ValueError(
                        f"{where} 的 when 引用了未知变量 {path[0]}{hint}；"
                        f"可用：user_input、upstream.<outputKey>"
                    )
                if len(path) == 1:
                    continue
                if path[1] not in keys:
                    raise ValueError(
                        f"{where} 的 when 引用了不存在的 upstream.{path[1]}；"
                        f"本图可用的 outputKey：{'、'.join(sorted(keys))}"
                    )

    def _validate_template_vars(self) -> None:
        """校验 ``inputTemplate`` 里用到的变量在保存时就能解析。

        运行期对未知变量的处理是「原样保留」，这是**故意**的：模板由人写，
        一次拼错不该让整条 run 失败。但原样保留的代价是 ``{{pan}}`` 会原封不动
        出现在发给模型的 prompt 里，看起来像模型在胡说。把它挡在保存时，用户
        改个字段名就行，总比事后对着 prompt 猜要省事。

        顺带拦 ``{{upstream.plan}}`` 这种带点的写法：模板语法里点号没有意义，
        它几乎总是「以为能这么写」的误解，直接告诉正确写法。
        """
        dotted = re.findall(r"\{\{\s*([\w.]+)\s*\.\w+\s*\}\}", self.inputTemplate)
        if dotted:
            raise ValueError(
                f"inputTemplate 里的 {{{{{dotted[0]}.xxx}}}} 不支持点号访问；"
                f"单独引用某个上游产出请直接写 {{{{{dotted[0]}}}}}，"
                f"全部上游写 {{{{upstream}}}}"
            )

        known = {"user_input", "upstream"} | {n.outputKey or n.id for n in self.nodes}
        unknown = sorted(self.template_vars() - known)
        if unknown:
            raise ValueError(
                f"inputTemplate 引用了未知变量：{'、'.join(unknown)}；"
                f"可用：user_input、upstream、"
                f"{'、'.join(sorted(known - {'user_input', 'upstream'})) or '（本图暂无上游产出）'}"
            )

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

        DAG 下同层会有多个节点（并行分支），它们的**相对**先后由声明顺序决定，
        但它们会被 LangGraph 放进同一个超步并发跑，实际不构成先后。

        条件边在拓扑序里和普通边一视同仁：它决定了「最坏情况下会经过哪些节点」，
        这是 ``recursion_limit`` 需要的上界。
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

    def topo_order(self) -> list[WorkflowNode]:
        """公开的拓扑序。

        V1/V2 的 ``linear_order()`` 在名字和语义上都只适用于「一条链」，V3 支持
        DAG 之后继续用它会误导（调用方多半以为返回的就是真实执行顺序，而 DAG
        下并行分支并没有唯一顺序），所以改名。旧名字已移除——留一个会抛
        ``ValueError`` 的空壳只会让调用方以为是图非法。
        """
        return self._topo_order()

    def template_vars(self) -> set[str]:
        """收集 ``inputTemplate`` 里用到的全部 ``{{变量}}``。

        由 :meth:`_validate_template_vars` 在保存时消费：``user_input`` /
        ``upstream`` 是保留变量，其余名字必须落在某个节点的 outputKey 上，否则
        运行期只会把 ``{{xxx}}`` 原样发给模型看。
        """
        return set(_VAR_RE.findall(self.inputTemplate))

    def render_input(self, user_input: str, upstream: dict[str, Any]) -> str:
        """渲染 inputTemplate。

        三类变量同时可用：

        - ``{{user_input}}``：本次用户输入；
        - ``{{upstream}}``：上游产出的 Markdown 摘要（按 outputKey 分块）；
        - ``{{outputKey}}``：**逐个**上游产出，例如 ``{{left_out}}``。

        第三类是 DAG 下的刚需：并行分支同时产出多份内容，用 ``{{upstream}}``
        一把全塞进 prompt 的话，模型分不清哪段对应哪个分支。

        未知变量保持原样（交给 :func:`render_template` 的既定行为）而不是替换成
        空串——拼错的名字要能一眼看出来，静默抹掉只会得到一个莫名其妙少了一段
        的 prompt。保存期的 ``_validate_template_vars`` 会把拼错挡在前面。
        """
        variables: dict[str, Any] = dict(upstream)
        variables["user_input"] = user_input
        variables["upstream"] = _render_upstream(upstream)
        return render_template(self.inputTemplate, variables)


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
