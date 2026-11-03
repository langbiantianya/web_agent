"""沙箱 runner 的纯逻辑（skill-service §6、§9）。"""

from __future__ import annotations

import os
from io import BytesIO

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


def test_parse_oom_kill_count_from_cgroup_v2():
    """cgroup v2 的 ``oom_kill`` 计数能被解析。"""
    text = "low 0\nhigh 0\nmax 0\noom 1\noom_kill 3\n"
    assert runner._parse_oom_kill_count(text) == 3


def test_oom_signal_with_cgroup_evidence_is_limited(monkeypatch):
    """脚本被 OOM killer 选中时，不能只报模糊的 SIGKILL。"""

    class _KilledProcess:
        """已以 SIGKILL 退出的假进程。"""

        def __init__(self, *_args, **_kwargs):
            """构造可直接被读取的管道。"""
            self.returncode = -9
            self.stdin = BytesIO()
            self.stdout = BytesIO()
            self.stderr = BytesIO()

        def poll(self):
            """立即报告已被信号杀掉。"""
            return -9

    counts = iter((7, 8))
    monkeypatch.setattr(runner.subprocess, "Popen", _KilledProcess)
    monkeypatch.setattr(runner, "_oom_kill_count", lambda: next(counts))
    result = runner.run_script(
        {
            "language": "python",
            "content": "x = b'x' * 1024\n",
            "argv": [],
            "stdin": "",
            "timeout_ms": 5000,
            "limits": {"stdout_bytes": 1024, "stderr_bytes": 1024},
        }
    )
    assert result["status"] == "limited"
    assert result["error_code"] == "sandbox_oom"
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
