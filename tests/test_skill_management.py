"""Skill 管理后台所需的运行档状态与导入风险可视化。"""

from __future__ import annotations

import base64
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config.settings import settings
from app.core.skill_risk import scan_skill_risks
from app.main import create_app


def test_skill_risk_scanner_reports_imports_network_and_shebang():
    """导入扫描只提示风险，不执行脚本。"""
    code = b"""#!/bin/sh
import requests
requests.get("https://example.com")
"""
    warnings = scan_skill_risks(
        "正文",
        [
            ("scripts/run.py", code),
            ("scripts/fetch.sh", b"#!/bin/bash\ncurl https://example.com\n"),
            ("scripts/local.py", b"from helper import value\n"),
            ("scripts/helper.py", b"value = 1\n"),
        ],
    )

    text = "\n".join(warnings)
    assert "shebang 与扩展名不一致" in text
    assert "第三方模块 import「requests」" in text
    assert "网络访问痕迹" in text
    assert "helper" not in text


def test_import_entries_exposes_warnings_and_runtime_status():
    """导入响应要带风险提示，列表要带当前运行档可用性。"""
    client = TestClient(create_app())
    headers = {"X-Admin-Key": settings.admin_key}
    code = f"risk-{uuid4().hex[:8]}"
    skill_md = (
        "---\n"
        f"name: {code}\n"
        "description: 风险导入测试\n"
        "metadata:\n"
        "  runtime-profile: isolated\n"
        "  runtime-image: ml-py311\n"
        "allowed-tools: Bash(git:*)\n"
        "---\n\n测试。\n"
    )
    script = "import requests\nrequests.get('https://example.com')\n"
    payload = {
        "entries": [
            {
                "path": "SKILL.md",
                "content_base64": base64.b64encode(skill_md.encode()).decode(),
            },
            {
                "path": "scripts/run.py",
                "content_base64": base64.b64encode(script.encode()).decode(),
            },
        ]
    }

    response = client.post(
        "/api/skills/import/entries", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["runtime_profile"] == "isolated"
    assert body["runtime_image"] == "ml-py311"
    assert body["runtime_available"] is False
    assert any("requests" in item for item in body["import_warnings"])

    listed = client.get("/api/skills?limit=500")
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["code"] == code)
    assert row["allowed_tools"] == "Bash(git:*)"
    assert row["runtime_profile"] == "isolated"
    assert row["runtime_available"] is False
    assert "studio" in row["runtime_detail"]

    assert client.delete(
        f"/api/skills/{code}", headers=headers
    ).status_code == 204


def test_client_service_does_not_serve_legacy_admin_page():
    """Skill 管理页改由独立 Ludic admin-web 提供。"""
    client = TestClient(create_app())
    page = client.get("/admin")

    assert page.status_code == 404
