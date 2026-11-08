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
from dataclasses import dataclass
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
    # 展示用名称。运行期不读它（产出键仍取 outputKey 或 id），
    # 但画布上必须靠它区分两个同名的审批节点。
    label: str = Field(default="", max_length=128)
    agentCode: str = Field(default="", max_length=64)
    outputKey: str = Field(default="", max_length=64)
    # 审批节点上给审批人看的话；只有 type=approval 时才有意义
    prompt: str = Field(default="", max_length=2000)
    # 会签人数；同一审批人只能投一票。
    requiredApprovals: int = Field(default=1, ge=1, le=20)
    # 0 表示不超时。超时动作默认驳回归属于 fail-closed。
    approvalTimeoutSec: int = Field(default=0, ge=0, le=30 * 24 * 3600)
    approvalTimeoutAction: str = Field(default="reject", max_length=16)

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
            if (
                self.requiredApprovals != 1
                or self.approvalTimeoutSec != 0
                or self.approvalTimeoutAction != "reject"
            ):
                raise ValueError(
                    f"节点 {self.id} 不是 approval 类型，不能配置审批会签或超时"
                )
            return self
        if not self.prompt:
            raise ValueError(f"节点 {self.id} 是 approval 类型，必须写 prompt")
        if self.agentCode:
            raise ValueError(
                f"节点 {self.id} 是 approval 类型，不能同时指定 agentCode"
            )
        if self.approvalTimeoutAction not in {"reject", "approve"}:
            raise ValueError(
                f"节点 {self.id} 的 approvalTimeoutAction 只支持 reject / approve"
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


@dataclass(frozen=True)
class GraphIssue:
    """一条图结构问题，附带定位信息。

    ``message`` 与改造前 ``ValueError`` 的文本逐字一致——它是给管理员看的
    唯一文案来源，前端不另造说法。

    ``node_ids`` / ``edge_refs`` 让画布能把错误高亮到具体节点或边，
    而不是让用户在一句中文里自己找名字。
    """

    message: str
    node_ids: tuple[str, ...] = ()
    edge_refs: tuple[str, ...] = ()


def collect_graph_issues(payload: dict) -> list[GraphIssue]:
    """收集 definition 的全部图结构问题。

    规则、检查顺序与错误文案与 ``WorkflowDefinition`` 校验期完全一致；
    区别只在于这里**收集全部**问题并带上定位，而不是遇到第一条就抛。

    这里是图结构规则的**唯一一份实现**：保存期的 ``WorkflowDefinition``
    校验与画布的实时校验都走它，不存在「运行期一套、校验端点一套」的
    分叉。

    输入是**任意**客户端 JSON，形状不一定对得上 definition：``nodes`` /
    ``edges`` 可能不是数组，元素可能不是对象，节点也可能没有 id。畸形部分
    由 :func:`_sanitize_payload` 挑出来单独报成 issue，剩下的才交给图规则
    ——图规则因此不必到处防着畸形输入，畸形部分也不会从错误列表里消失。
    """
    normalized, shape_issues = _sanitize_payload(payload)
    issues: list[GraphIssue] = list(shape_issues)
    ids = {n["id"] for n in normalized["nodes"]}
    entry = str(normalized.get("entry") or "")
    edges = normalized["edges"]

    for edge in edges:
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        if src not in ids or dst not in ids:
            issues.append(
                GraphIssue(
                    message=f"edge 引用了不存在的节点：{src} -> {dst}",
                    node_ids=tuple(sorted({src, dst} - ids)),
                    edge_refs=(f"{src}->{dst}",),
                )
            )
    if entry and entry not in ids:
        issues.append(
            GraphIssue(
                message=f"entry 节点不存在：{entry}",
                node_ids=(entry,),
            )
        )

    issues.extend(_detect_cycle(normalized))
    issues.extend(_detect_orphans(normalized))
    issues.extend(_detect_fanin_shape(normalized))
    issues.extend(_detect_outgoing_mixed(normalized))
    issues.extend(_detect_condition_problems(normalized))
    issues.extend(_detect_template_problems(normalized))
    return issues


def _sanitize_payload(payload: dict) -> tuple[dict, list[GraphIssue]]:
    """把任意客户端 JSON 归一成图规则能安全处理的形状。

    ``collect_graph_issues`` 是校验端点唯一的规则来源，而它拿到的是**任意**
    客户端 JSON：``nodes`` / ``edges`` 可能不是数组，元素可能不是对象，节点
    也可能没有合法的字符串 id。

    畸形部分**单独报成 issue**而不是静默跳过——静默跳过会让畸形节点从错误
    列表里消失，用户以为图是干净的。

    模型路径（``WorkflowDefinition`` 构造出的实例）上 pydantic 已保证
    ``nodes`` / ``edges`` 都是对象数组、节点都带非空字符串 id，因此这里的每
    一项检查在模型路径上都是 no-op：产出的 issue 恒为空，归一后的
    ``nodes`` / ``edges`` 与输入逐元素相同。
    """
    issues: list[GraphIssue] = []

    if not isinstance(payload, dict):
        return {"nodes": [], "edges": []}, [
            GraphIssue(message="definition 必须是对象")
        ]

    raw_nodes = payload.get("nodes")
    if raw_nodes is None:
        raw_nodes = []
    elif not isinstance(raw_nodes, list):
        issues.append(GraphIssue(message="nodes 必须是数组"))
        raw_nodes = []

    raw_edges = payload.get("edges")
    if raw_edges is None:
        raw_edges = []
    elif not isinstance(raw_edges, list):
        issues.append(GraphIssue(message="edges 必须是数组"))
        raw_edges = []

    # 节点没有合法 id 就没法参与图结构计算：与其当成 id="None" 让两个缺 id 的
    # 节点互相顶替，不如单独报出来。
    nodes: list[dict] = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            issues.append(GraphIssue(message=f"nodes[{index}] 不是对象，已跳过"))
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            issues.append(GraphIssue(message=f"nodes[{index}] 缺少合法 id，已跳过"))
            continue
        nodes.append(node)

    # 边的 from/to 缺失不影响安全：后面的「edge 引用了不存在的节点」规则会
    # 自然接手，不必在这里另立一条。
    edges: list[dict] = []
    for index, edge in enumerate(raw_edges):
        if not isinstance(edge, dict):
            issues.append(GraphIssue(message=f"edges[{index}] 不是对象，已跳过"))
            continue
        edges.append(edge)

    return {**payload, "nodes": nodes, "edges": edges}, issues


def _detect_cycle(payload: dict) -> list[GraphIssue]:
    """检测环。

    拓扑排序必须能覆盖全部节点，否则执行顺序无法确定（还会触发
    ``recursion_limit``）。

    消息与改造前的 ``ValueError`` 文本逐字一致。
    """
    # 节点 id 去重：adj/indeg 是按 id 建键的字典，重复 id 若留在 ids 里会让
    # len(ids) 虚高，把无环的图误判成有环。改造前这里用的是 {n.id for n in nodes}。
    ids = list(dict.fromkeys(str(n.get("id")) for n in payload.get("nodes") or []))
    adj: dict[str, list[str]] = {i: [] for i in ids}
    indeg: dict[str, int] = {i: 0 for i in ids}
    for edge in payload.get("edges") or []:
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        if src in adj and dst in adj:
            adj[src].append(dst)
            indeg[dst] += 1
    ready = [i for i in ids if indeg[i] == 0]
    seen = 0
    while ready:
        cur = ready.pop(0)
        seen += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if seen != len(ids):
        return [GraphIssue(message="definition 存在环，无法确定执行顺序")]
    return []


def _detect_orphans(payload: dict) -> list[GraphIssue]:
    """检测从 entry 走不到的孤立节点。

    孤立节点是最阴险的一种配置错误：保存不报错、图能编译，但那个节点永远
    不会被执行，用户却以为它在流程里。与其在运行日志里让人猜，不如在保存
    时直接指出来。

    消息与改造前的 ``ValueError`` 文本逐字一致。
    """
    ids = {str(n.get("id")) for n in payload.get("nodes") or []}
    if not ids:
        return []
    nodes = payload.get("nodes") or []
    entry = str(payload.get("entry") or "") or str(nodes[0].get("id"))
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for edge in payload.get("edges") or []:
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        if src in adj and dst in adj:
            adj[src].append(dst)
    seen = {entry}
    queue = [entry]
    while queue:
        cur = queue.pop()
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    orphans = ids - seen
    if orphans:
        return [
            GraphIssue(
                message=(
                    "以下节点从 entry 走不到（孤立节点）："
                    + "、".join(sorted(orphans))
                ),
                node_ids=tuple(sorted(orphans)),
            )
        ]
    return []


def _detect_fanin_shape(payload: dict) -> list[GraphIssue]:
    """检测汇聚节点入边混条件边。

    LangGraph 对多入边节点的处理是「等齐所有入边才触发」。若其中一条是
    条件边而这次没命中，汇聚节点就会永远等不到——run 静默地少跑一段，
    最后还以 done 收尾。

    例外是「所有入边都是条件边」：那就得靠运行期再判一次，超出本版范围，
    因此直接拒绝并说明怎么办。

    消息与改造前的 ``ValueError`` 文本逐字一致。
    """
    ids = {str(n.get("id")) for n in payload.get("nodes") or []}
    incoming: dict[str, list[dict]] = {i: [] for i in ids}
    for edge in payload.get("edges") or []:
        dst = str(edge.get("to") or "")
        if dst in incoming:
            incoming[dst].append(edge)
    issues: list[GraphIssue] = []
    for node_id, edges in incoming.items():
        if len(edges) < 2:
            continue
        conditional = [e for e in edges if e.get("when")]
        if not conditional:
            continue
        refs = tuple(
            f"{e.get('from')}->{node_id}" for e in conditional
        )
        if len(conditional) < len(edges):
            bad = "、".join(f"{e.get('from')} --when--> {node_id}" for e in conditional)
            issues.append(
                GraphIssue(
                    message=(
                        f"节点 {node_id} 是汇聚节点（{len(edges)} 条入边），"
                        f"但入边里混了条件边（{bad}）：条件不成立时它会永远等不到。"
                        f"请把入边全部改成无条件边，或让条件节点直接连到 {node_id} 的下游。"
                    ),
                    node_ids=(node_id,),
                    edge_refs=refs,
                )
            )
        else:
            issues.append(
                GraphIssue(
                    message=(
                        f"节点 {node_id} 的 {len(edges)} 条入边全是条件边："
                        f"本版不支持「多条条件边汇聚到同一节点」，请改成无条件汇聚。"
                    ),
                    node_ids=(node_id,),
                    edge_refs=refs,
                )
            )
    return issues


def _detect_outgoing_mixed(payload: dict) -> list[GraphIssue]:
    """检测同一节点出边混用条件与无条件。

    混用时「无条件那部分到底还走不走」要靠读者自己推断，语义不明确；
    LangGraph 虽然能跑，但排查起来很痛苦。明确二选一更好。

    消息与改造前的 ``ValueError`` 文本逐字一致。
    """
    outgoing: dict[str, list[dict]] = {}
    for edge in payload.get("edges") or []:
        outgoing.setdefault(str(edge.get("from") or ""), []).append(edge)
    issues: list[GraphIssue] = []
    for node_id, edges in outgoing.items():
        conditional = [e for e in edges if e.get("when")]
        if conditional and len(conditional) < len(edges):
            issues.append(
                GraphIssue(
                    message=(
                        f"节点 {node_id} 的出边里条件边与无条件边混用："
                        f"请统一成「全无条件（并行）」或「全条件（分支）」。"
                    ),
                    node_ids=(node_id,),
                )
            )
    return issues


def _detect_condition_problems(payload: dict) -> list[GraphIssue]:
    """检测 when 表达式的语法错误与未知变量。

    变量校验是这里最有价值的一条：``upstream.plan`` 里 ``plan`` 拼错、或引用了
    一个已被删掉的节点，表达式照样能通过语法检查，运行期只会安静地求值成
    ``None`` → 条件为假 → **走错分支**。分支选错不会有任何报错，只会在生产里
    表现为「为什么走了 else 分支」，所以这类错误必须挡在保存时。

    语法只认两种写法：``user_input`` 与 ``upstream.<outputKey>``。刻意**不**
    放行裸 ``plan``：求值期的 scope 只有这两个根（见 run_graph 的路由函数），
    裸名会求值成 ``None``；而放行它还会引出「某个节点恰好叫 ``user_input``」
    这类遮蔽问题。与其留一个静默为假的写法，不如保存时直接告诉正确写法。

    消息与改造前的 ``ValueError`` 文本逐字一致。
    """
    keys = {
        str(n.get("outputKey") or n.get("id"))
        for n in payload.get("nodes") or []
    }
    issues: list[GraphIssue] = []
    for edge in payload.get("edges") or []:
        when = str(edge.get("when") or "")
        if not when:
            continue
        src = str(edge.get("from") or "")
        dst = str(edge.get("to") or "")
        ref = f"{src}->{dst}"
        where = f"边 {src} -> {dst}"
        try:
            expr.check_syntax(when)
        except expr.ExprError as exc:
            issues.append(
                GraphIssue(
                    message=f"{where} 的 when 表达式有语法错误：{exc}",
                    node_ids=(src, dst),
                    edge_refs=(ref,),
                )
            )
            continue
        for path in expr.variables(when):
            if path[0] == "user_input":
                continue
            if path[0] != "upstream":
                hint = f"；请写成 upstream.{path[0]}" if path[0] in keys else ""
                issues.append(
                    GraphIssue(
                        message=(
                            f"{where} 的 when 引用了未知变量 {path[0]}{hint}；"
                            f"可用：user_input、upstream.<outputKey>"
                        ),
                        node_ids=(src, dst),
                        edge_refs=(ref,),
                    )
                )
                break
            if len(path) == 1:
                continue
            if path[1] not in keys:
                issues.append(
                    GraphIssue(
                        message=(
                            f"{where} 的 when 引用了不存在的 upstream.{path[1]}；"
                            f"本图可用的 outputKey：{'、'.join(sorted(keys))}"
                        ),
                        node_ids=(src, dst),
                        edge_refs=(ref,),
                    )
                )
                break
    return issues


def _detect_template_problems(payload: dict) -> list[GraphIssue]:
    """检测 inputTemplate 里的未知变量与点号写法。

    运行期对未知变量的处理是「原样保留」，这是**故意**的：模板由人写，
    一次拼错不该让整条 run 失败。但原样保留的代价是 ``{{pan}}`` 会原封不动
    出现在发给模型的 prompt 里，看起来像模型在胡说。把它挡在保存时，用户
    改个字段名就行，总比事后对着 prompt 猜要省事。

    顺带拦 ``{{upstream.plan}}`` 这种带点的写法：模板语法里点号没有意义，
    它几乎总是「以为能这么写」的误解，直接告诉正确写法。

    消息与改造前的 ``ValueError`` 文本逐字一致。
    """
    template = str(payload.get("inputTemplate") or "")
    if not template:
        return []
    dotted = re.findall(r"\{\{\s*([\w.]+)\s*\.\w+\s*\}\}", template)
    if dotted:
        return [
            GraphIssue(
                message=(
                    f"inputTemplate 里的 {{{{{dotted[0]}.xxx}}}} 不支持点号访问；"
                    f"单独引用某个上游产出请直接写 {{{{{dotted[0]}}}}}，"
                    f"全部上游写 {{{{upstream}}}}"
                )
            )
        ]
    known = {"user_input", "upstream"} | {
        str(n.get("outputKey") or n.get("id")) for n in payload.get("nodes") or []
    }
    used = set(_VAR_RE.findall(template))
    unknown = sorted(used - known)
    if unknown:
        return [
            GraphIssue(
                message=(
                    f"inputTemplate 引用了未知变量：{'、'.join(unknown)}；"
                    f"可用：user_input、upstream、"
                    f"{'、'.join(sorted(known - {'user_input', 'upstream'})) or '（本图暂无上游产出）'}"
                )
            )
        ]
    return []


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

        规则实现在 :func:`collect_graph_issues` 里——**只有一份**。
        这里只负责把它转成 pydantic 能包住的 ``ValueError``，且消息文本与
        改造前逐字一致，既有测试断言不需要改。

        Raises:
            ValueError: 图结构有问题。由 pydantic 包装为 ValidationError 上抛。
        """
        payload = self.model_dump(by_alias=True, exclude_none=True)
        issues = collect_graph_issues(payload)
        if issues:
            raise ValueError(issues[0].message)
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

        由 :func:`collect_graph_issues` 在保存时消费：``user_input`` /
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
        的 prompt。保存期的 ``collect_graph_issues`` 会把拼错挡在前面。
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
