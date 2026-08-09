import os
import pytest
from autoswe.sandbox import SandboxRunner


def test_sandbox_isolated_command_execution(tmp_path):
    runner = SandboxRunner(work_dir=str(tmp_path))
    res = runner.run_command("python3 -c 'print(10 + 20)'")
    assert res["exit_code"] == 0
    assert "30" in res["stdout"]


def test_sandbox_timeout_enforcement(tmp_path):
    runner = SandboxRunner(work_dir=str(tmp_path), timeout_sec=1)
    res = runner.run_command("python3 -c 'import time; time.sleep(5)'")
    assert res["exit_code"] != 0
    assert "TIMED_OUT" in res["stderr"] or res["exit_code"] == 124


def test_sandbox_env_cleansing(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super_secret_aws_key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-123456")
    monkeypatch.setenv("SAFE_ENV_VAR", "safe_val")

    runner = SandboxRunner(work_dir=str(tmp_path))
    res = runner.run_command(
        "python3 -c 'import os; print(os.environ.get(\"AWS_SECRET_ACCESS_KEY\"), os.environ.get(\"OPENAI_API_KEY\"), os.environ.get(\"SAFE_ENV_VAR\"))'"
    )
    assert res["exit_code"] == 0
    assert "None None safe_val" in res["stdout"]
