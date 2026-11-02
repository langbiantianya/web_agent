"""studio 的启动入口。

用法::

    python -m services.studio
"""

from __future__ import annotations

import asyncio

from services.studio import serve


def main() -> None:
    """启动 studio。"""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
