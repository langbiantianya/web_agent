"""studio 镜像的**运行期依赖**必须齐（设计文档 §22.8）。

## 这条测试是为了钉住一个真踩过的坑

studio 的镜像**只装** ``services/studio/requirements.txt`` 里那几个包——它是独立
部署的，不装 web_agent 的依赖。

而生成的桩代码 ``webagent/studio/v1/studio_pb2.py`` 里写的是
``from google.protobuf import descriptor``——**protobuf 是运行期依赖**。

第一次写 ``services/studio/requirements.txt`` 时只写了 ``grpcio``。本地测试
全绿（开发机上的 grpcio-tools 把 protobuf 一起装了），**容器里直接炸**：

    ModuleNotFoundError: No module named 'google'

这类 bug 本地测不出来——只有真构建一次镜像才会暴露。所以这里做**静态**检查：
把 studio 代码里 import 的每个第三方顶层模块，都要求能在
``services/studio/requirements.txt`` 列出的包里找到。

## 判定口径

- 只看 studio **自己的**代码（``services/studio/`` 与生成桩 ``webagent/studio/v1/``）；
- 本地包（``services`` / ``webagent``）与标准库不算；
- 用 ``importlib.metadata`` 把顶层模块映射到发行包名——``google`` 映射到
  ``protobuf``，``grpc`` 映射到 ``grpcio``，靠猜是猜不出来的。
"""

from __future__ import annotations

import ast
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# studio 侧要检查的代码。
STUDIO_SOURCES = [
    ROOT / "services" / "studio",
    ROOT / "webagent" / "studio" / "v1",
]

# studio 自己的包——它们不是第三方依赖。
LOCAL_PACKAGES = {"services", "webagent"}


def _normalize(name: str) -> str:
    """把发行包名归一化（PEP 503：大写、下划线、连字符等价）。

    Args:
        name: 原始名字。

    Returns:
        归一化后的名字。
    """
    return name.lower().replace("_", "-").replace(".", "-")


def _requirements() -> set[str]:
    """读 studio 的运行期依赖清单。

    Returns:
        归一化后的发行包名集合。
    """
    text = (ROOT / "services" / "studio" / "requirements.txt").read_text(
        encoding="utf-8"
    )
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # 去掉版本约束与 extras
        name = line.split("=")[0].split(">")[0].split("<")[0].split("[")[0].strip()
        if name:
            out.add(_normalize(name))
    return out


def _imported_modules() -> set[str]:
    """扫出 studio 代码 import 的所有顶层模块。

    Returns:
        顶层模块名集合。
    """
    modules: set[str] = set()
    for target in STUDIO_SOURCES:
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        modules.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.level:  # 相对 import
                        continue
                    if node.module:
                        modules.add(node.module.split(".")[0])
    return modules


def _third_party(modules: set[str]) -> set[str]:
    """挑出其中的第三方模块（排除标准库与本地包）。

    Args:
        modules: 顶层模块名集合。

    Returns:
        第三方模块名集合。
    """
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    return {
        m for m in modules if m not in stdlib and m not in LOCAL_PACKAGES
    }


def test_studio_runtime_dependencies_are_complete():
    """studio 代码 import 的每个第三方模块，都得由 requirements.txt 里的包提供。"""
    required = _requirements()
    third_party = _third_party(_imported_modules())
    assert third_party, "没扫到第三方 import——扫描逻辑坏了，不是依赖齐了"

    # 模块 → 发行包名。用 importlib.metadata 而不是猜（google → protobuf）。
    provided_by = metadata.packages_distributions()

    missing: list[str] = []
    for module in sorted(third_party):
        dists = provided_by.get(module) or []
        if not dists:
            # 装都装不上的模块：要么是环境里没装，要么是真缺。
            # 判成缺，让它显式出现在 requirements.txt 里。
            missing.append(f"{module}（当前环境里找不到提供它的发行包）")
            continue
        # 只要有一个发行包在清单里就算齐（同一个模块可能由多个发行包提供）。
        if not any(_normalize(d) in required for d in dists):
            missing.append(
                f"{module} → {'、'.join(dists)}（不在 services/studio/requirements.txt 里）"
            )

    assert not missing, (
        "studio 的运行期依赖不齐——镜像只装 requirements.txt 里的包，"
        "缺一个就是容器起不来（本地测试看不出来，开发机恰好装过）：\n  "
        + "\n  ".join(missing)
    )


def test_generated_stub_requires_protobuf_at_runtime():
    """生成的桩 import 的是 ``google.protobuf``——protobuf 必须是显式依赖。

    这条单独立出来，是因为它是最容易被漏掉的那一个：``grpcio-tools`` 会把
    protobuf 一起装上，于是**看起来**有依赖，实际上运行期的镜像里没有。
    """
    required = _requirements()
    assert "protobuf" in required, (
        "protobuf 是运行期依赖（生成桩 import google.protobuf），"
        "不能靠 grpcio-tools 传递带进来"
    )


@pytest.mark.parametrize(
    "module,expect_dist",
    [
        ("grpc", "grpcio"),
        ("google", "protobuf"),
    ],
)
def test_known_module_to_distribution_mapping(module: str, expect_dist: str):
    """钉住「顶层模块 → 发行包名」的映射。

    这条不是测业务，是测**判定逻辑本身**：如果 mapping 变了（比如 protobuf
    改名），上面那条会因为映射失效而误报。
    """
    provided_by = metadata.packages_distributions()
    dists = provided_by.get(module) or []
    assert expect_dist in {_normalize(d) for d in dists}, (
        f"{module} 的提供方变成了 {dists}，判定逻辑要跟着改"
    )
