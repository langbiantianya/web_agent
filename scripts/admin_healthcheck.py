"""admin-web 容器健康检查。"""

from __future__ import annotations

import json
import os
import urllib.request


def main() -> int:
    """请求本机 /admin/health。"""
    port = os.environ.get("ADMIN_WEB_PORT", "8001")
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/admin/health", timeout=5
        ) as response:
            payload = json.loads(response.read())
        return 0 if payload.get("status") == "ok" else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
