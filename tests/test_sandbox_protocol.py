"""沙箱输入/输出协议（skill-service §9）。"""

from __future__ import annotations

import base64

import pytest

from services.sandbox.protocol import (
    FRAME_MARKER,
    FrameError,
    SandboxSpec,
    build_payload,
    encode_frames,
    parse_frames,
    parse_payload,
)


def test_payload_roundtrip():
    """spec → payload → dict，字段不丢。"""
    spec = SandboxSpec(
        skill="demo",
        script="scripts/a.py",
        language="python",
        content="print('hi')",
        argv=("--x", "1"),
        stdin="in",
        timeout_ms=5000,
        image="ml",
        request_id="req-1",
    )
    data = parse_payload(build_payload(spec, "nonce-1"))
    assert data["nonce"] == "nonce-1"
    assert data["content"] == "print('hi')"
    assert data["argv"] == ["--x", "1"]
    assert data["stdin"] == "in"
    assert data["timeout_ms"] == 5000


def test_payload_rejects_bad_json():
    """坏 JSON 要在 dispatcher 侧就拒，不能进沙箱。"""
    with pytest.raises(FrameError):
        parse_payload(b"{not json")


def test_payload_rejects_empty_content():
    """空脚本没有执行意义。"""
    with pytest.raises(FrameError):
        parse_payload(
            b'{"nonce":"n","content":"  ","language":"python","argv":[],"stdin":""}'
        )


def test_frames_roundtrip_keeps_utf8_and_digest():
    """中文/换行输出原样回来，摘要一致。"""
    stdout = "第一行\n第二行\n".encode("utf-8")
    stderr = "警告".encode("utf-8")
    text = encode_frames(
        nonce="n1",
        exit_code=3,
        truncated=False,
        stdout=stdout,
        stderr=stderr,
        duration_ms=12,
        cpu_seconds=0.125,
        peak_memory_bytes=1024,
    )
    parsed = parse_frames(text)
    assert parsed["exit_code"] == 3
    assert parsed["stdout"] == stdout
    assert parsed["stderr"] == stderr
    assert parsed["status"] == "completed"
    assert parsed["duration_ms"] == 12
    assert parsed["cpu_seconds"] == 0.125
    assert parsed["peak_memory_bytes"] == 1024


def test_frames_missing_end_is_rejected():
    """没有 END 帧 → 结果不可信。"""
    text = encode_frames(
        nonce="n2", exit_code=0, truncated=False, stdout=b"x", stderr=b""
    )
    lines = [line for line in text.split("\n") if " END " not in line]
    with pytest.raises(FrameError):
        parse_frames("\n".join(lines))


def test_frames_digest_mismatch_is_rejected():
    """内容与摘要对不上 → 结果不可信。"""
    text = encode_frames(
        nonce="n3", exit_code=0, truncated=False, stdout=b"abc", stderr=b""
    )
    text = text.replace(
        base64.b64encode(b"abc").decode(), base64.b64encode(b"abd").decode()
    )
    with pytest.raises(FrameError):
        parse_frames(text)


def test_split_line_is_merged():
    """运行时在中间切行时，非标记行要接回上一行。"""
    stdout = b"y" * 10
    text = encode_frames(
        nonce="n4", exit_code=0, truncated=False, stdout=stdout, stderr=b""
    )
    lines = text.split("\n")
    # 把 OUT 那一行从中间切成两行
    out_index = next(i for i, line in enumerate(lines) if " OUT " in line)
    line = lines[out_index]
    cut = len(line) // 2
    lines[out_index : out_index + 1] = [line[:cut], line[cut:]]
    parsed = parse_frames("\n".join(lines))
    assert parsed["stdout"] == stdout


def test_unknown_frame_kind_is_ignored():
    """向前兼容：不认识的帧类型忽略。"""
    text = encode_frames(
        nonce="n5", exit_code=0, truncated=False, stdout=b"x", stderr=b""
    ).replace(" END ", f" OTHER x\n{FRAME_MARKER} n5 END ")
    parsed = parse_frames(text)
    assert parsed["stdout"] == b"x"
