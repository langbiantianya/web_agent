"""``python -m services.dispatcher`` 的入口。"""

from __future__ import annotations

import asyncio

from services.dispatcher.server import serve


def main() -> None:
    """跑 dispatcher。"""
    asyncio.run(serve())


if __name__ == "__main__":
    main()
