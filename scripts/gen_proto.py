"""生成 gRPC 桩代码。

契约文件只有一份：``proto/webagent/studio/v1/studio.proto``。改契约的流程是

1. 改 proto 文件；
2. **同步改两份文档里贴的 proto 片段**（``app/DESIGN.md`` §22.8.3 与
   ``services/studio/DESIGN.md`` §5）——三处必须逐字一致；
3. 跑本脚本重新生成桩代码；
4. 跑 ``python scripts/check_proto_contract.py`` 确认三处仍然一致。

用法::

    python scripts/gen_proto.py

生成物落在仓库根的 ``webagent/studio/v1/`` 下——目录名就是 proto 里的
``package webagent.studio.v1;``，所以 ``protoc`` 生成的
``studio_pb2_grpc.py`` 内部的 ``from webagent.studio.v1 import studio_pb2``
能直接解析，**一行生成代码都不用改**。

studio 服务与 web_agent 共用这一份：studio 容器里不装 web_agent 的代码，
把这个目录拷进去就能用（主设计 §22.8.3：契约文件在仓库里只有一份）。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTO_ROOT = ROOT / "proto"
PROTO_FILE = PROTO_ROOT / "webagent" / "studio" / "v1" / "studio.proto"
OUT_ROOT = ROOT

# 需要 __init__.py 的包目录（proto 包名的每一级）。
PACKAGE_DIRS = [
    ROOT / "webagent",
    ROOT / "webagent" / "studio",
    ROOT / "webagent" / "studio" / "v1",
]

INIT_TEMPLATE = '''"""``{pkg}`` 包的生成桩代码（不要手改，见 :mod:`webagent`）。"""
'''


def main() -> int:
    """生成桩代码。返回进程退出码。

    Returns:
        0 表示成功；非 0 表示失败。
    """
    if not PROTO_FILE.exists():
        print(f"找不到契约文件：{PROTO_FILE}", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_ROOT}",
        f"--python_out={OUT_ROOT}",
        f"--grpc_python_out={OUT_ROOT}",
        str(PROTO_FILE.relative_to(PROTO_ROOT)).replace("\\", "/"),
    ]
    print("执行：", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        return proc.returncode

    for directory in PACKAGE_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
        init = directory / "__init__.py"
        if not init.exists():
            pkg = ".".join(directory.relative_to(ROOT).parts)
            init.write_text(INIT_TEMPLATE.format(pkg=pkg), encoding="utf-8", newline="\n")
            print(f"补齐 {init.relative_to(ROOT)}")

    generated = sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in (ROOT / "webagent").rglob("*.py")
    )
    print("生成物：")
    for name in generated:
        print("  ", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
