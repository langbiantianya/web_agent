"""检查指定范围内的函数是否都带 docstring。

用法：
    python scripts/check_docstrings.py            # 全量
    python scripts/check_docstrings.py app/core   # 指定目录
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}

# 默认扫描范围。这里必须显式列出根目录下的模块（run.py）——只扫子目录的话，
# 根目录新增的脚本会静默漏检，"0 缺失"就成了假象。
DEFAULT_TARGETS = ["run.py", "app", "scripts", "alembic", "tests"]


def _expand(target: str) -> list[Path]:
    """把命令行参数展开成文件列表。

    要同时接受三种写法：目录（递归）、.py 文件、以及省略扩展名的模块路径
    （如 ``app/cache`` → ``app/cache.py`` 或 ``app/cache/__init__.py``）。
    """
    p = Path(target)
    if p.is_dir():
        return sorted(p.rglob("*.py"))
    if p.is_file():
        return [p]
    for candidate in (p.with_suffix(".py"), p / "__init__.py"):
        if candidate.is_file():
            return [candidate]
    raise FileNotFoundError(f"找不到路径（既不是目录也不是 .py 文件）：{target}")


def collect(targets: list[str]) -> tuple[int, list[tuple[str, int, str]]]:
    """统计目标范围内的函数总数，以及缺失 docstring 的那些（含文件、行号、函数名）。

    返回 ``(总数, 缺失列表)``；缺失列表为空即代表全部函数都有 docstring。
    """
    total = 0
    missing: list[tuple[str, int, str]] = []
    for target in targets:
        for f in _expand(target):
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                print(f"!! 语法错误 {f}: {exc}")
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    total += 1
                    if not ast.get_docstring(node):
                        missing.append((f.as_posix(), node.lineno, node.name))
    return total, missing


def main() -> int:
    """命令行入口。打印统计并列出缺失项；全部齐全返回 0，否则返回 1。"""
    targets = sys.argv[1:] or DEFAULT_TARGETS
    total, missing = collect(targets)
    print(f"函数总数: {total}    缺 docstring: {len(missing)}")
    if missing:
        print()
        by_file: dict[str, list[tuple[int, str]]] = {}
        for f, lineno, name in missing:
            by_file.setdefault(f, []).append((lineno, name))
        for f in sorted(by_file):
            print(f"{len(by_file[f]):3d}  {f}")
            for lineno, name in by_file[f]:
                print(f"        L{lineno:<5d} {name}")
        return 1
    print("✅ 全部函数均已带 docstring")
    return 0


if __name__ == "__main__":
    sys.exit(main())
