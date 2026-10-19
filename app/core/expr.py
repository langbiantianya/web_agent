"""条件边的表达式求值器（设计文档 §15.4）。

## 为什么要手写解析器

``definition`` 里的 ``when`` 表达式由用户在后台页面上写，直接 ``eval()`` 等于把
RCE 能力交给任何能调 ``PUT /api/workflows/{code}`` 的人。这里用一个
**固定文法的递归下降解析器**：它只能产生比较与布尔运算，做不到调函数、访问
属性、导入模块——不是靠黑名单拦，而是根本没有那些语法。

## 文法

.. code-block:: text

    expr     := or_expr
    or_expr  := and_expr ("or" and_expr)*
    and_expr := not_expr ("and" not_expr)*
    not_expr := "not" not_expr | cmp
    cmp      := primary (("==" | "!=" | ">" | "<" | ">=" | "<=") primary)?
    primary  := STRING | NUMBER | "true" | "false" | "null"
               | path | func "(" [expr ("," expr)*] ")" | "(" expr ")"
    path     := IDENT ("." IDENT)*
    func     := "len" | "contains" | "lower" | "is_empty"

取值语义：变量不存在时求值为 ``None``（而不是抛错）——这样「某个上游还没产出」
这种状态可以被 ``is_empty(x)`` 正常表达，而不会把整个 run 掀翻。

比较规则刻意保守：**类型不同一律为 False，不抛错**。把数字和字符串比大小在
任何语言里都没有明确答案，与其猜一个方向，不如让条件不成立走 else 分支——
那是用户能看见、能改的行为。真正的拼写错误由配置期校验拦（见
:meth:`WorkflowDefinition._validate_conditions`），不会拖到运行期。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["ExprError", "check_syntax", "evaluate", "variables"]


class ExprError(ValueError):
    """表达式语法错误。

    继承 ``ValueError`` 是为了让 pydantic 把它包成 ``ValidationError``，
    从而走既有的 400 响应体，而不是变成 500。
    """


# ---------------------------------------------------------------- 词法

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<string>"[^"]*"|'[^']*')
    | (?P<number>\d+\.\d+|\d+)
    | (?P<dot>\.)
    | (?P<op>==|!=|>=|<=|>|<|\(|\)|,)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)

_RESERVED_WORDS = {"and", "or", "not", "true", "false", "null"}

#: 允许调用的函数 -> (最小参数个数, 最大参数个数)。刻意做成白名单：解析器只认
#: 这几个标识符是函数，其余 ``IDENT`` 一律当变量路径，遇到调用直接语法错误。
#: 参数个数也在这里定死，这样 ``contains(x)`` 这种漏参数的写法在**保存配置时**
#: 就被拒，而不是等到某个 run 真的走到这条边才炸。
_FUNCS = {
    "len": (1, 1),
    "contains": (2, 2),
    "lower": (1, 1),
    "is_empty": (1, 1),
}

_FUNC_USAGE = {
    "len": "len(x)",
    "contains": "contains(文本, 子串)",
    "lower": "lower(文本)",
    "is_empty": "is_empty(x)",
}

_MAX_LEN = 1000


def _tokenize(src: str) -> list[tuple[str, str]]:
    """把源码切成 ``(kind, value)`` 列表。非法字符在这里就被拒掉。"""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if m is None:
            raise ExprError(f"无法识别的字符 {src[pos]!r}（位置 {pos}）")
        pos = m.end()
        kind = m.lastgroup
        value = m.group()
        if kind == "ws":
            continue
        tokens.append((kind, value))
    tokens.append(("eof", ""))
    return tokens


# ---------------------------------------------------------------- 语法树

# 节点用元组表示： ("lit", value) / ("var", [名字...]) / ("call", 名, [子节点])
#                   ("cmp", op, 左, 右) / ("and"|"or", [子节点...]) / ("not", 子节点)


class _Parser:
    """递归下降解析器。实例只服务于一次解析，用完即弃。"""

    def __init__(self, src: str) -> None:
        """切词并把游标停在起点。"""
        self.tokens = _tokenize(src)
        self.pos = 0

    def peek(self) -> tuple[str, str]:
        """看当前 token 但不消费。"""
        return self.tokens[self.pos]

    def next(self) -> tuple[str, str]:
        """消费并返回当前 token。"""
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def accept_keyword(self, word: str) -> bool:
        """若当前是关键字就吃掉并返回 True；否则不动。"""
        kind, value = self.peek()
        if kind == "ident" and value == word:
            self.pos += 1
            return True
        return False

    def parse(self):
        """解析完整表达式，确保 token 全部用完。"""
        node = self.parse_or()
        kind, value = self.peek()
        if kind != "eof":
            raise ExprError(f"表达式结尾有多余内容：{value!r}")
        return node

    def parse_or(self):
        """``or``：最松的结合优先级，左右结合。"""
        parts = [self.parse_and()]
        while self.accept_keyword("or"):
            parts.append(self.parse_and())
        return parts[0] if len(parts) == 1 else ("or", parts)

    def parse_and(self):
        """``and``。"""
        parts = [self.parse_not()]
        while self.accept_keyword("and"):
            parts.append(self.parse_not())
        return parts[0] if len(parts) == 1 else ("and", parts)

    def parse_not(self):
        """``not`` 前缀。"""
        if self.accept_keyword("not"):
            return ("not", self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self):
        """比较运算。只能有一个——``a == b == c`` 直接报错而不是悄悄截断。"""
        left = self.parse_primary()
        kind, value = self.peek()
        if kind == "op" and value in ("==", "!=", ">", "<", ">=", "<="):
            self.next()
            right = self.parse_primary()
            kind2, value2 = self.peek()
            if kind2 == "op" and value2 in ("==", "!=", ">", "<", ">=", "<="):
                raise ExprError("比较运算符不能连续使用（请加括号明确优先级）")
            return ("cmp", value, left, right)
        return left

    def parse_primary(self):
        """字面量、变量路径、函数调用、括号组。"""
        kind, value = self.next()

        if kind == "string":
            return ("lit", value[1:-1])

        if kind == "number":
            return ("lit", float(value) if "." in value else int(value))

        if kind == "ident":
            if value == "true":
                return ("lit", True)
            if value == "false":
                return ("lit", False)
            if value == "null":
                return ("lit", None)
            if value in _RESERVED_WORDS:
                raise ExprError(f"关键字 {value!r} 不能放在这里")
            # 函数调用
            k2, v2 = self.peek()
            if k2 == "op" and v2 == "(":
                if value not in _FUNCS:
                    raise ExprError(
                        f"不支持的函数 {value!r}；可用：{', '.join(sorted(_FUNCS))}"
                    )
                self.next()
                args: list = []
                if not (self.peek()[0] == "op" and self.peek()[1] == ")"):
                    args.append(self.parse_or())
                    while self.peek() == ("op", ","):
                        self.next()
                        args.append(self.parse_or())
                self.expect(")")
                lo, hi = _FUNCS[value]
                if not lo <= len(args) <= hi:
                    raise ExprError(
                        f"{value}() 需要 "
                        f"{lo if lo == hi else f'{lo}~{hi}'} 个参数，实际 {len(args)} 个"
                        f"；用法 {_FUNC_USAGE[value]}"
                    )
                return ("call", value, args)
            return ("var", self.parse_path_tail([value]))

        if kind == "op" and value == "(":
            node = self.parse_or()
            self.expect(")")
            return node

        raise ExprError(f"意外的符号 {value!r}")

    def parse_path_tail(self, names: list[str]) -> list[str]:
        """把 ``a.b.c`` 的后缀接在已读的首段名字后面。"""
        while self.peek()[0] == "dot":
            self.next()
            kind, value = self.next()
            if kind != "ident":
                raise ExprError(f"'.' 后面必须是标识符，实际是 {value!r}")
            names.append(value)
        return names

    def expect(self, token: str) -> None:
        """断言下一个 token 是指定符号，否则报错。"""
        kind, value = self.next()
        if kind != "op" or value != token:
            raise ExprError(f"缺少 {token!r}，实际是 {value!r}")


# ---------------------------------------------------------------- 求值


def _lookup(scope: dict[str, Any], path: list[str]) -> Any:
    """按 ``a.b.c`` 取值；任何一段不存在都返回 ``None`` 而不是抛错。"""
    cur: Any = scope
    for name in path:
        if isinstance(cur, dict):
            cur = cur.get(name)
        else:
            return None
        if cur is None:
            return None
    return cur


def _compare(op: str, left: Any, right: Any) -> bool:
    """比较。类型不同一律 False，见模块 docstring 的说明。"""
    if op in ("==", "!="):
        equal = left == right
        return equal if op == "==" else not equal

    # 有序比较：两边都必须是「可比的」。数字与数字比数值，字符串与字符串比字典序。
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        pass
    elif isinstance(left, str) and isinstance(right, str):
        pass
    else:
        return False

    if op == ">":
        return left > right
    if op == "<":
        return left < right
    if op == ">=":
        return left >= right
    return left <= right


def _call(name: str, args: list[Any]) -> Any:
    """执行白名单函数。参数个数不对时抛错——那是配置写错了，不是数据问题。"""
    if name == "len":
        if len(args) != 1:
            raise ExprError("len() 只接受 1 个参数")
        x = args[0]
        if x is None:
            return 0
        try:
            return len(x)
        except TypeError:
            return 0
    if name == "contains":
        if len(args) != 2:
            raise ExprError("contains() 需要 2 个参数：contains(文本, 子串)")
        hay, needle = args[0], args[1]
        if hay is None or needle is None:
            return False
        return str(needle) in str(hay)
    if name == "lower":
        if len(args) != 1:
            raise ExprError("lower() 只接受 1 个参数")
        return "" if args[0] is None else str(args[0]).lower()
    if name == "is_empty":
        if len(args) != 1:
            raise ExprError("is_empty() 只接受 1 个参数")
        x = args[0]
        if x is None:
            return True
        if isinstance(x, str):
            return not x.strip()
        return len(x) == 0 if hasattr(x, "__len__") else False
    raise ExprError(f"不支持的函数 {name!r}")  # pragma: no cover - 解析期已拦


def _eval(node, scope: dict[str, Any]) -> Any:
    """递归求值。"""
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "var":
        return _lookup(scope, node[1])
    if kind == "not":
        return not _truthy(_eval(node[1], scope))
    if kind == "and":
        # 短路：左边为假就不再看右边
        for sub in node[1]:
            if not _truthy(_eval(sub, scope)):
                return False
        return True
    if kind == "or":
        for sub in node[1]:
            if _truthy(_eval(sub, scope)):
                return True
        return False
    if kind == "cmp":
        return _compare(node[1], _eval(node[2], scope), _eval(node[3], scope))
    if kind == "call":
        return _call(node[1], [_eval(a, scope) for a in node[2]])
    raise ExprError(f"未知的节点类型 {kind!r}")  # pragma: no cover


def _truthy(v: Any) -> bool:
    """把求值结果转成条件真假。

    空串与 ``None`` 算假——上游节点输出了空白时，「有内容」的条件应当不成立。
    """
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return bool(v)


# ---------------------------------------------------------------- 公开接口


def check_syntax(src: str) -> None:
    """只做语法检查，不求值。配置期用它把拼错的表达式挡在保存之前。"""
    if not src or not src.strip():
        raise ExprError("条件表达式为空")
    if len(src) > _MAX_LEN:
        raise ExprError(f"条件表达式过长（{len(src)} > {_MAX_LEN} 字符）")
    _Parser(src).parse()


def evaluate(src: str, scope: dict[str, Any]) -> bool:
    """求值并返回布尔结果。

    Args:
        src: 表达式源码。
        scope: 变量作用域。约定含一个 ``upstream`` dict：``outputKey`` → 节点产出。

    Returns:
        条件是否成立。

    Raises:
        ExprError: 语法错误（正常情况下配置期已拦过，运行期出现说明校验漏了）。
    """
    return _truthy(_eval(_Parser(src).parse(), scope))


def variables(src: str) -> list[list[str]]:
    """收集表达式引用了哪些变量路径，供配置期校验比对。

    只收集**根名字**（``upstream.a.b`` 收成 ``["upstream", "a", "b"]`` 完整路径，
    但校验方一般只关心根）。返回完整路径列表，去重。
    """
    seen: list[list[str]] = []

    def walk(node) -> None:
        """深度优先遍历 AST 收集 var 节点。"""
        if node[0] == "var":
            if node[1] not in seen:
                seen.append(node[1])
        elif node[0] in ("and", "or"):
            for sub in node[1]:
                walk(sub)
        elif node[0] == "not":
            walk(node[1])
        elif node[0] == "cmp":
            walk(node[2])
            walk(node[3])
        elif node[0] == "call":
            for sub in node[2]:
                walk(sub)

    walk(_Parser(src).parse())
    return seen
