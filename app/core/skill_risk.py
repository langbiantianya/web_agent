"""技能脚本的导入期静态风险提示。"""

from __future__ import annotations

import ast
import sys
from pathlib import PurePosixPath


def _script_kind(path: str) -> str | None:
    """按扩展名判断脚本类型。"""
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".py": "python",
        ".sh": "bash",
        ".bash": "bash",
        ".js": "node",
        ".mjs": "node",
    }.get(suffix)


def _shebang_mismatch(path: str, text: str) -> str | None:
    """检查 shebang 是否与扩展名匹配。"""
    first = text.splitlines()[0] if text else ""
    if not first.startswith("#!"):
        return None
    lower = first.lower()
    kind = _script_kind(path)
    expected = {
        "python": ("python",),
        "bash": ("bash", "/sh"),
        "node": ("node",),
    }.get(kind or "", ())
    if expected and not any(token in lower for token in expected):
        return f"{path}：shebang 与扩展名不一致（{first.strip()}）"
    return None


def _python_imports(path: str, text: str, local_modules: set[str]) -> list[str]:
    """列出 Python 文件里的第三方顶层导入。"""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        return [f"{path}：Python 语法无法静态解析（第 {exc.lineno or '?'} 行）"]
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".", 1)[0])
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    risky = sorted(modules - stdlib - local_modules)
    return [f"{path}：第三方模块 import「{name}」" for name in risky]


def _network_hints(path: str, text: str) -> list[str]:
    """识别常见网络访问调用，提示需要确认 isolated 与网络策略。"""
    lower = text.lower()
    markers = {
        "curl ": "curl",
        "wget ": "wget",
        "requests.": "requests",
        "httpx.": "httpx",
        "urllib.request": "urllib.request",
        "socket.": "socket",
        "fetch(": "fetch",
        "axios.": "axios",
        "http.client": "http.client",
    }
    found = [label for marker, label in markers.items() if marker in lower]
    return [f"{path}：检测到网络访问痕迹「{label}」" for label in found]


def scan_skill_risks(
    body: str, resources: list[tuple[str, bytes]]
) -> list[str]:
    """扫描技能包中的脚本导入、网络调用与 shebang 风险。

    这里只做静态提示，不执行脚本、不做安全结论。运行期授权仍由工具白名单、
    studio profile 与 sandbox 网络隔离决定。

    Args:
        body: 技能正文。
        resources: ``[(path, content), ...]``。

    Returns:
        去重后的风险提示列表，最多 50 条。
    """
    del body  # 当前只扫描可执行资源；正文由敏感内容过滤器处理。
    local_modules = {
        PurePosixPath(path).stem
        for path, _ in resources
        if path.endswith(".py")
    }
    warnings: list[str] = []
    for path, raw in resources:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        mismatch = _shebang_mismatch(path, text)
        if mismatch:
            warnings.append(mismatch)
        if _script_kind(path) == "python":
            warnings.extend(_python_imports(path, text, local_modules))
        warnings.extend(_network_hints(path, text))
    return list(dict.fromkeys(warnings))[:50]
