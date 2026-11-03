"""沙箱 runner 的纯逻辑（skill-service §6、§9）。"""

from __future__ import annotations

import os

import pytest

from services.sandbox import runner


def test_interpreter_mapping():
    """三种语言各对应一个解释器。"""
    assert runner._interpreter("python")[0] == runner.sys.executable
    assert runner._interpreter("bash") == ["/bin/bash"]
    assert runner._interpreter("node") == ["node"]
    with pytest.raises(ValueError):
        runner._interpreter("ruby")


def test_env_is_whitelisted(monkeypatch):
    """环境变量只带白名单；HOME/TMPDIR 指向临时目录。"""
    monkeypatch.setenv("SECRET_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = runner._env("/tmp/sandbox-x")
    assert "SECRET_KEY" not in env
    assert env["HOME"] == "/tmp/sandbox-x"
    assert env["TMPDIR"] == "/tmp/sandbox-x"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_unknown_language_fails_without_spawning():
    """不认识的运行时直接报 failed，不会去起进程。"""
    result = runner.run_script(
        {"language": "ruby", "content": "puts 1", "argv": [], "stdin": ""}
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "unknown_language"
    assert result["exit_code"] == -1


@pytest.mark.skipif(os.name != "posix", reason="Windows 沙箱里起不了子进程")
def test_python_script_runs_and_frames_parse():
    """真跑一个 python 脚本，控制帧能解析回原文。"""
    from services.sandbox.protocol import parse_frames

    result = runner.run_script(
        {
            "language": "python",
            "content": "print('hello')\nimport sys\nsys.stderr.write('warn')\n",
            "argv": [],
            "stdin": "",
            "timeout_ms": 5000,
            "limits": {"stdout_bytes": 1024, "stderr_bytes": 1024},
        }
    )
    text = runner.encode_frames(nonce="n", **result)
    parsed = parse_frames(text)
    assert parsed["stdout"] == b"hello\n"
    assert parsed["stderr"] == b"warn"
    assert parsed["exit_code"] == 0


@pytest.mark.skipif(os.name != "posix", reason="Windows 沙箱里起不了子进程")
def test_output_cap_marks_truncated():
    """输出超限要标 truncated，并把进程杀掉。"""
    result = runner.run_script(
        {
            "language": "python",
            "content": "print('x' * 10000)\n",
            "argv": [],
            "stdin": "",
            "timeout_ms": 5000,
            "limits": {"stdout_bytes": 100, "stderr_bytes": 100},
        }
    )
    assert result["truncated"] is True
    assert result["status"] == "limited"
