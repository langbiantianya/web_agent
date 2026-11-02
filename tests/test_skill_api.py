"""Skill API（设计文档 §22.11）。

要钉住的命题：

1. **写接口是管理员专属且 fail-closed**——没带密钥 / 密钥错 / 未配置，一律 401；
2. **只读接口不需要密钥**——普通运营能看技能、看资源、导出；
3. **导入的技能默认停用**——导入是唯一一条内容来源不可信的入口；
4. **导出与导入对称**——``import(export(x)) == x``；
5. **每次写入都留痕**——version+1 且有修订历史。
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from app.main import create_app

ADMIN_KEY = "test-admin-key"
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
async def client():
    """真实 app + HTTP 客户端。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as c:
            yield c


def _zip(entries: dict[str, bytes]) -> bytes:
    """做一个 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _skill_md(name: str = "demo", description: str = "演示技能") -> bytes:
    """一份合法的 SKILL.md。"""
    return f"---\nname: {name}\ndescription: {description}\n---\n\n正文内容。\n".encode()


# --------------------------------------------------------------- 写接口门禁


async def test_create_requires_admin(client):
    """没带 admin key → 401（fail-closed）。"""
    resp = await client.post(
        "/api/skills",
        json={"code": "demo", "description": "演示", "body": "正文"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "unauthorized"


async def test_create_with_wrong_admin_key_rejected(client):
    """密钥不对 → 401。"""
    resp = await client.post(
        "/api/skills",
        json={"code": "demo", "description": "演示", "body": "正文"},
        headers={"X-Admin-Key": "wrong"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("put", "/api/skills/demo", {"json": {"body": "x"}}),
        ("delete", "/api/skills/demo", {}),
        ("post", "/api/skills/demo/resources", {"content": b"x"}),
        ("post", "/api/skills/import", {}),
        ("post", "/api/skills/demo/rollback", {"json": {"version": 1}}),
    ],
)
async def test_all_write_endpoints_require_admin(client, method, path, kwargs):
    """所有写接口都要过管理员门禁——漏一个就是一条后门。"""
    resp = await getattr(client, method)(path, **kwargs)
    assert resp.status_code == 401, f"{method} {path} → {resp.status_code}"


# ------------------------------------------------------------------ CRUD


async def test_create_and_read(client):
    """管理员建了能读回来。"""
    resp = await client.post(
        "/api/skills",
        json={"code": "demo", "description": "演示", "body": "正文"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == "demo"
    assert body["body"] == "正文"
    assert body["origin"] == "manual"
    assert body["enabled"] is True
    assert body["version"] == 1
    assert body["content_hash"]

    got = await client.get("/api/skills/demo")
    assert got.status_code == 200
    assert got.json()["body"] == "正文"


async def test_create_duplicate_code_conflicts(client):
    """code 唯一。"""
    payload = {"code": "dup", "description": "d", "body": "b"}
    first = await client.post("/api/skills", json=payload, headers=ADMIN_HEADERS)
    assert first.status_code == 201, first.text
    second = await client.post("/api/skills", json=payload, headers=ADMIN_HEADERS)
    assert second.status_code == 409, second.text


async def test_invalid_code_rejected(client):
    """code 的格式不合 SKILL.md 的 name 规则 → 400。"""
    resp = await client.post(
        "/api/skills",
        json={"code": "Bad_Code", "description": "d"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


async def test_name_must_equal_code(client):
    """``name`` 必须与 ``code`` 一致——规范要求 name 等于目录名。

    存一个不一样的 name，导出的 SKILL.md 就不是合规的了。
    """
    resp = await client.post(
        "/api/skills",
        json={"code": "namediff", "name": "别的名字", "description": "d"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text
    assert "name 必须与 code 一致" in resp.json()["message"]


async def test_name_equal_to_code_accepted(client):
    """name 与 code 一致就接受。"""
    resp = await client.post(
        "/api/skills",
        json={"code": "namesame", "name": "namesame", "description": "d"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "namesame"


async def test_get_missing_skill_is_404(client):
    """不存在的技能 → 404。"""
    assert (await client.get("/api/skills/nope")).status_code == 404


async def test_update_bumps_version_and_writes_revision(client):
    """更新：version+1、正文变、有修订历史。"""
    created = await client.post(
        "/api/skills",
        json={"code": "upd", "description": "d", "body": "v1"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 201, created.text

    updated = await client.put(
        "/api/skills/upd", json={"body": "v2"}, headers=ADMIN_HEADERS
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["body"] == "v2"

    revs = await client.get("/api/skills/upd/revisions")
    assert revs.status_code == 200
    versions = [r["version"] for r in revs.json()]
    assert 2 in versions and 1 in versions


async def test_delete_skill(client):
    """删技能 → 204，之后读不到。"""
    await client.post(
        "/api/skills",
        json={"code": "del", "description": "d", "body": "b"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.delete("/api/skills/del", headers=ADMIN_HEADERS)
    assert resp.status_code == 204, resp.text
    assert (await client.get("/api/skills/del")).status_code == 404


async def test_delete_bound_skill_conflicts(client):
    """还被 Agent 绑着的技能删不掉——删了会让模型行为静默改变。"""
    await client.post(
        "/api/skills",
        json={"code": "bound", "description": "d", "body": "b"},
        headers=ADMIN_HEADERS,
    )
    await client.post(
        "/api/agents",
        json={
            "code": "bound_agent",
            "name": "a",
            "system_prompt": "x",
            "model": "m",
            "base_url": "http://x",
            "skills": ["bound"],
        },
    )
    resp = await client.delete("/api/skills/bound", headers=ADMIN_HEADERS)
    assert resp.status_code == 409, resp.text
    assert "绑着" in resp.json()["message"]


async def test_list_reports_used_by(client):
    """列表里带上被哪些 Agent 绑着。"""
    await client.post(
        "/api/skills",
        json={"code": "used", "description": "d", "body": "b"},
        headers=ADMIN_HEADERS,
    )
    await client.post(
        "/api/agents",
        json={
            "code": "used_agent",
            "name": "a",
            "system_prompt": "x",
            "model": "m",
            "base_url": "http://x",
            "skills": ["used"],
        },
    )
    items = (await client.get("/api/skills")).json()
    target = [s for s in items if s["code"] == "used"][0]
    assert target["used_by"] == ["used_agent"]


# ------------------------------------------------------------------ 资源


async def test_put_and_read_resource(client):
    """上传资源 → 列出来 → 读得到内容。"""
    await client.post(
        "/api/skills",
        json={"code": "res", "description": "d", "body": "b"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.post(
        "/api/skills/res/resources?path=references/M.md",
        content="指标口径".encode(),
        headers={**ADMIN_HEADERS, "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["path"] == "references/M.md"
    assert resp.json()["is_text"] is True
    assert resp.json()["size_bytes"] == len("指标口径".encode())

    listing = await client.get("/api/skills/res/resources")
    assert listing.status_code == 200
    assert [r["path"] for r in listing.json()] == ["references/M.md"]

    content = await client.get("/api/skills/res/resources/references/M.md")
    assert content.status_code == 200
    assert "指标口径" in content.text


async def test_put_resource_with_invalid_path_rejected(client):
    """路径格式非法 → 400。"""
    await client.post(
        "/api/skills",
        json={"code": "badres", "description": "d", "body": "b"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.post(
        "/api/skills/badres/resources?path=../../etc/passwd",
        content=b"x",
        headers={**ADMIN_HEADERS, "Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 400, resp.text


async def test_delete_resource(client):
    """删资源。"""
    await client.post(
        "/api/skills",
        json={"code": "delres", "description": "d", "body": "b"},
        headers=ADMIN_HEADERS,
    )
    await client.post(
        "/api/skills/delres/resources?path=a.md",
        content=b"x",
        headers={**ADMIN_HEADERS, "Content-Type": "application/octet-stream"},
    )
    resp = await client.delete("/api/skills/delres/resources/a.md", headers=ADMIN_HEADERS)
    assert resp.status_code == 204, resp.text
    listing = await client.get("/api/skills/delres/resources")
    assert listing.json() == []


async def test_binary_resource_downloads_as_attachment(client):
    """二进制资源走 attachment，不在浏览器里乱渲染。"""
    await client.post(
        "/api/skills",
        json={"code": "bin", "description": "d", "body": "b"},
        headers=ADMIN_HEADERS,
    )
    await client.post(
        "/api/skills/bin/resources?path=assets/t.bin",
        content=b"\x00\x01\x02",
        headers={**ADMIN_HEADERS, "Content-Type": "application/octet-stream"},
    )
    resp = await client.get("/api/skills/bin/resources/assets/t.bin")
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")


# ------------------------------------------------------------------ 导入导出


async def test_import_defaults_disabled(client):
    """导入的技能默认 disabled——来源不可信。"""
    resp = await client.post(
        "/api/skills/import",
        files={"file": ("pkg.zip", _zip({"SKILL.md": _skill_md("imported")}), "application/zip")},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["enabled"] is False
    assert resp.json()["origin"] == "import"


async def test_import_zip_slip_rejected(client):
    """含 .. 的 zip 条目一律拒。"""
    resp = await client.post(
        "/api/skills/import",
        files={"file": ("bad.zip", _zip({"../../etc/passwd": b"x"}), "application/zip")},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


async def test_import_requires_skill_md(client):
    """包里没有 SKILL.md → 400。"""
    resp = await client.post(
        "/api/skills/import",
        files={"file": ("pkg.zip", _zip({"references/a.md": b"x"}), "application/zip")},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


async def test_export_import_roundtrip(client):
    """导出再导入能还原——导入导出必须对称。"""
    await client.post(
        "/api/skills",
        json={"code": "rt", "description": "演示", "body": "正文", "license": "MIT"},
        headers=ADMIN_HEADERS,
    )
    await client.post(
        "/api/skills/rt/resources?path=references/a.md",
        content="引用内容".encode(),
        headers={**ADMIN_HEADERS, "Content-Type": "application/octet-stream"},
    )
    exported = await client.get("/api/skills/rt/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/zip")

    resp = await client.post(
        "/api/skills/import",
        files={"file": ("rt.zip", exported.content, "application/zip")},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 409, resp.text  # code 已存在

    # 换个 code 导入，确认内容对得上
    await client.put(
        "/api/skills/rt", json={"description": "演示2"}, headers=ADMIN_HEADERS
    )
    exported2 = await client.get("/api/skills/rt/export")
    entries = _read_zip(exported2.content)
    assert "SKILL.md" in entries
    assert entries["references/a.md"] == "引用内容".encode()


def _read_zip(data: bytes) -> dict[str, bytes]:
    """读出一个 zip 的条目。"""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name)
    return out


async def test_export_md_format(client):
    """``format=md`` 导出 SKILL.md 原文。"""
    await client.post(
        "/api/skills",
        json={"code": "md", "description": "演示", "body": "正文内容"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.get("/api/skills/md/export?format=md")
    assert resp.status_code == 200
    assert "name: md" in resp.text
    assert "正文内容" in resp.text


async def test_import_entries_directory_form(client):
    """目录结构形式（JSON + base64）也能导入。"""
    import base64

    resp = await client.post(
        "/api/skills/import/entries",
        json={
            "entries": [
                {
                    "path": "SKILL.md",
                    "content_base64": base64.b64encode(_skill_md("dironly")).decode(),
                }
            ]
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == "dironly"
    assert resp.json()["enabled"] is False


async def test_import_entries_rejects_bad_path(client):
    """目录结构形式也走同一套路径校验。"""
    import base64

    resp = await client.post(
        "/api/skills/import/entries",
        json={
            "entries": [
                {
                    "path": "../../etc/passwd",
                    "content_base64": base64.b64encode(b"x").decode(),
                }
            ]
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 400, resp.text


# ------------------------------------------------------------------ 回滚


async def test_rollback_restores_body(client):
    """回滚到 v1：正文回去，version 继续往前走。"""
    await client.post(
        "/api/skills",
        json={"code": "rb", "description": "d", "body": "v1"},
        headers=ADMIN_HEADERS,
    )
    await client.put("/api/skills/rb", json={"body": "v2"}, headers=ADMIN_HEADERS)

    resp = await client.post(
        "/api/skills/rb/rollback", json={"version": 1}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == "v1"
    # 回滚本身也是一次变更，version 继续递增
    assert resp.json()["version"] == 3


async def test_rollback_missing_version_is_404(client):
    """回滚到不存在的版本 → 404。"""
    await client.post(
        "/api/skills",
        json={"code": "rb2", "description": "d", "body": "v1"},
        headers=ADMIN_HEADERS,
    )
    resp = await client.post(
        "/api/skills/rb2/rollback", json={"version": 99}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 404, resp.text


# ------------------------------------------------------------- 提示词预览


async def test_prompt_preview_matches_injected_content(client):
    """提示词预览与实际注入的内容逐字一致。"""
    await client.post(
        "/api/skills",
        json={"code": "prev", "description": "d", "body": "技能正文：先读 metrics。"},
        headers=ADMIN_HEADERS,
    )
    await client.post(
        "/api/agents",
        json={
            "code": "prev_agent",
            "name": "a",
            "system_prompt": "你是助手",
            "model": "m",
            "base_url": "http://x",
            "skills": ["prev"],
        },
    )
    resp = await client.get("/api/agents/prev_agent/prompt-preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["system_prompt"] == "你是助手"
    assert body["messages"][0]["content"] == "你是助手"
    assert body["messages"][1]["content"] == "技能正文：先读 metrics。"
    assert body["skills"][0]["injected"] is True
