"""算术表达式求值（内置工具 ``calc`` 的后端，设计文档 §17）。

## 为什么不用 eval

``eval("__import__('os').system('rm -rf /')")`` 是一个完全合法的「表达式」。
工具参数来自**模型**，而模型会被提示词注入影响——等于把 RCE 暴露给任何能写
prompt 的人。本项目已有同样的先例：条件表达式不用 ``eval``（见
:mod:`app.core.expr`），而工具参数的危险等级只比条件表达式更高。

## 为什么用 ast 模块，而不是手写递归下降

``ast.parse`` 只**解析**、不求值；求值由本模块自己的白名单遍历完成。能力上
等价于手写解析器，但边界情况由 Python 自己的语法定义——手写解析器最容易漏的
恰恰是那些奇怪的合法写法（科学计数法、Unicode 数字、隐式括号……）。

安全边界是「白名单」而不是「黑名单」：只有 :data:`_BIN_OPS` / :data:`_UNARY_OPS`
里列出的运算、以及数字常量，才被接受；其余节点类型一律拒绝。将来 Python 出现
新的 AST 节点时也不会被静默放行。

## 资源上限

表达式求值看着无害，但 ``2 ** 10000000`` 之类能把 CPU 和内存吃光。这里限制
表达式长度、AST 节点数、指数大小与结果量级，让「一条消息里的一个参数」无法
变成一次拒绝服务。
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

# 表达式长度上限（字符）
MAX_EXPR_LEN = 200
# AST 节点总数上限
MAX_NODES = 100
# 指数上限：`2 ** 100` 已经是 30 位数，再大就没意义了
MAX_EXPONENT = 100
# 整数结果的二进制位数上限（约 308 位十进制）
MAX_RESULT_BITS = 1024
# 浮点结果的绝对值上限
MAX_RESULT_ABS = 1e308

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class ArithError(ValueError):
    """算术表达式非法，或超出资源上限。"""


def evaluate(expr: str) -> int | float:
    """求值一个算术表达式。

    Args:
        expr: 算术表达式字符串，例如 ``"(12 + 8) * 3"``。只支持数字、算术运算符
            与括号；变量、函数调用、比较、字符串全部拒绝。

    Returns:
        计算结果（int 或 float）。

    Raises:
        ArithError: 语法错误、含不允许的构造，或结果超出资源上限。
    """
    if not isinstance(expr, str):
        raise ArithError("表达式必须是字符串")
    src = expr.strip()
    if not src:
        raise ArithError("表达式为空")
    if len(src) > MAX_EXPR_LEN:
        raise ArithError(f"表达式过长（上限 {MAX_EXPR_LEN} 字符）")

    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ArithError(f"表达式语法错误：{exc.msg}") from exc

    if len(list(ast.walk(tree))) > MAX_NODES:
        raise ArithError(f"表达式过于复杂（节点数上限 {MAX_NODES}）")

    return _eval(tree.body)


def _eval(node: ast.AST) -> int | float:
    """递归求值：白名单外的节点类型一律拒绝。

    Args:
        node: 当前 AST 节点。

    Returns:
        该节点的值。

    Raises:
        ArithError: 遇到不允许的节点或运算，或结果超出上限。
    """
    if isinstance(node, ast.Constant):
        value = node.value
        # bool 是 int 的子类，但它不是「数字」——明确排除，免得 True + 1 这种
        # 混进算术里。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ArithError("只支持数字常量")
        return _check_magnitude(value)

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ArithError(f"不支持的运算符：{type(node.op).__name__}")
        left = _eval(node.left)
        right = _eval(node.right)
        if op is operator.pow:
            # 指数必须在**求值之后、幂运算之前**检查：``10 ** 10 ** 10`` 的内层
            # 先算成 10^10，正好被这条挡住，不会真的去算那个天文数字。
            if isinstance(right, float) and right != int(right):
                raise ArithError("指数必须是整数")
            if abs(right) > MAX_EXPONENT:
                raise ArithError(f"指数过大（上限 {MAX_EXPONENT}）")
        try:
            result = op(left, right)
        except ZeroDivisionError as exc:
            raise ArithError("除数为零") from exc
        except (OverflowError, ValueError) as exc:
            raise ArithError(f"计算出错：{exc}") from exc
        return _check_magnitude(result)

    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ArithError(f"不支持的一元运算符：{type(node.op).__name__}")
        return _check_magnitude(op(_eval(node.operand)))

    raise ArithError(f"表达式里不允许出现：{type(node).__name__}")


def _check_magnitude(value: Any) -> int | float:
    """把结果的大小限制在可控范围内。

    每一次运算后都过一遍，于是「用乘法一点点堆出大数」也会在中途被拦下，
    不会等到最后才炸。

    Args:
        value: 刚算出来的值。

    Returns:
        原样返回（通过检查时）。

    Raises:
        ArithError: 整数位数过多、浮点非有限或过大。
    """
    if isinstance(value, int):
        if value.bit_length() > MAX_RESULT_BITS:
            raise ArithError("结果过大")
        return value
    if not math.isfinite(value):
        raise ArithError("结果不是有限数")
    if abs(value) > MAX_RESULT_ABS:
        raise ArithError("结果过大")
    return value
