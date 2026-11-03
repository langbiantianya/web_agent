"""校验 proto 契约与两份文档里贴的片段**逐字一致**。

主设计 §22.8.3 与 skill-service §5 都写着同一句话：

> 两份文档贴的 proto 片段**必须逐字一致**——proto 文件在仓库里只有一份
> （``proto/webagent/studio/v1/studio.proto``），CI 会校验一致性。

契约散在三处（proto 文件 + 两份文档），任何一处漏改，调用方与服务方就会拿着
不同的契约说话——而且**不会报错**，只是字段对不上、或者某个字段被静默忽略。
所以这里做成一个能跑的脚本，让它进 CI。

用法::

    python scripts/check_proto_contract.py

退出码 0 = 一致；非 0 = 不一致（并打印差异）。
"""

from __future__ import annotations

import difflib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTO_FILE = ROOT / "proto" / "webagent" / "studio" / "v1" / "studio.proto"

# 两份文档：文件路径 + 该文档里 proto 片段所在的围栏。
DOCS = [
    ("app/DESIGN.md", "主设计文档 §22.8.3"),
    ("services/studio/DESIGN.md", "Skill 服务文档 §5"),
]

BLOCK = re.compile(r"```proto\n(.*?)```", re.DOTALL)


def extract_block(path: pathlib.Path) -> str | None:
    """抽出文档里第一个 ```` ```proto ```` 围栏块。

    Args:
        path: 文档路径。

    Returns:
        片段原文；找不到时返回 ``None``。
    """
    text = path.read_text(encoding="utf-8")
    match = BLOCK.search(text)
    return match.group(1) if match else None


def diff(a: str, b: str, a_name: str, b_name: str) -> str:
    """两个文本的统一 diff。

    Args:
        a: 文本 A。
        b: 文本 B。
        a_name: A 的名字。
        b_name: B 的名字。

    Returns:
        diff 文本（相同则为空串）。
    """
    return "".join(
        difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            a_name,
            b_name,
        )
    )


def main() -> int:
    """校验三处一致。返回进程退出码。

    Returns:
        0 = 一致；1 = 不一致；2 = 文件缺失。
    """
    if not PROTO_FILE.exists():
        print(f"找不到契约文件：{PROTO_FILE}", file=sys.stderr)
        return 2

    canonical = PROTO_FILE.read_text(encoding="utf-8")
    failures = 0

    for rel, label in DOCS:
        path = ROOT / rel
        if not path.exists():
            print(f"[FAIL] {label}：文件不存在 {rel}", file=sys.stderr)
            failures += 1
            continue
        block = extract_block(path)
        if block is None:
            print(f"[FAIL] {label}：{rel} 里没有 ```proto 围栏块", file=sys.stderr)
            failures += 1
            continue
        if block == canonical:
            print(f"[ OK ] {label}：{rel} 与契约文件逐字一致")
            continue
        failures += 1
        print(f"[FAIL] {label}：{rel} 与契约文件不一致", file=sys.stderr)
        print(
            diff(canonical, block, "studio.proto", rel),
            file=sys.stderr,
        )

    if failures:
        print(
            f"\n契约三处不一致（{failures} 处）。改契约要同时改：\n"
            "  1. proto/webagent/studio/v1/studio.proto\n"
            "  2. app/DESIGN.md 的 §22.8.3 proto 片段\n"
            "  3. services/studio/DESIGN.md 的 §5 proto 片段\n"
            "改完再跑 python scripts/gen_proto.py 重新生成桩代码。",
            file=sys.stderr,
        )
        return 1

    print("\n契约三处一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
