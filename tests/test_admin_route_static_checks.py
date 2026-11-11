"""管理端路由的**静态**守门测试（UX-40）。

内置浏览器逐按钮实测时抓到一个 P0：``routes/llm_write.py`` 用了
``LlmEndpointUpdate`` 却**没有 import 它**，于是「保存端点」一按就是 500。

这类问题单测抓不到——只有真的走到那行代码才会炸；而走到那行代码需要
浏览器或者一个完整的端到端用例。���是这里用 :mod:`symtable` 做**静态**
检查：模块里引用了、但既没有在本模块定义、也不是内置函数的全局名，
一律是 ``NameError`` 的种子。

这个检查覆盖整个 ``services/admin_web`` 树，任何模块漏 import 都会被
立刻指出来（模块 + 行号 + 名字）。
"""

from __future__ import annotations

import builtins
import symtable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROOT = ROOT / "services" / "admin_web"

# 这些名字是「运行时注入」或「按约定存在」的，不算漏 import。
_ALLOWED_GLOBALS = {
    "__name__",
    "__file__",
    "__doc__",
    "__package__",
    "__loader__",
    "__spec__",
    "__builtins__",
    "__annotations__",
    "__class__",
    "WindowsError",
    # ``from __future__ import annotations`` 引入的编译期名字。
    "__conditional_annotations__",
}


def _module_globals(table: symtable.SymbolTable) -> set[str]:
    """模块级作用域里**已定义**的名字。"""
    return {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }


def _undefined_names(source: str, filename: str) -> list[tuple[int, str]]:
    """返回 ``(行号, 名字)``：引用了但没有定义的全局名。"""
    table = symtable.symtable(source, filename, "exec")
    defined = _module_globals(table) | set(dir(builtins)) | _ALLOWED_GLOBALS
    problems: list[tuple[int, str]] = []
    # 递归所有作用域：管理端的处理器都嵌在 ``register()`` 里面，只看一层
    # 会漏掉真正的漏 import（``LlmEndpointUpdate`` 就是这么漏的）。
    stack = [table]
    while stack:
        scope = stack.pop()
        stack.extend(scope.get_children())
        for symbol in scope.get_symbols():
            name = symbol.get_name()
            if name in defined:
                continue
            # 只关心**真正解析到全局作用域**的引用：局部变量与闭包自由
            # 变量都被 symtable 归到别的类别，不在这里。
            if not symbol.is_global():
                continue
            if not symbol.is_referenced():
                continue
            problems.append((scope.get_lineno(), name))
    return sorted(set(problems))


def _admin_modules() -> list[Path]:
    return sorted(
        path
        for path in ADMIN_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


@pytest.mark.parametrize("path", _admin_modules(), ids=lambda p: p.name)
def test_admin_module_has_no_undefined_global_names(path: Path) -> None:
    """管理端每个模块都不许有「引用了却没定义」的全局名。

    漏 import 会在运行到那行时抛 ``NameError``——对管理端来说就是「某个
    按钮一按就 500」。这个检查把这类错误挡在提交之前。
    """
    source = path.read_text(encoding="utf-8")
    problems = _undefined_names(source, str(path))
    assert not problems, (
        f"{path.relative_to(ROOT)} 引用了没有定义的全局名："
        + "、".join(f"{name}（行 {line}）" for line, name in problems)
    )
