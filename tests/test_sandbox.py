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


def test_sandbox_dynamic_env_cleansing(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_SECRET", "secret123")
    monkeypatch.setenv("USER_PASSWORD", "pass123")
    monkeypatch.setenv("OAUTH_TOKEN", "tok123")
    monkeypatch.setenv("AUTH_BEARER", "auth123")
    monkeypatch.setenv("CLIENT_CREDENTIAL", "cred123")
    monkeypatch.setenv("PRIVATE_KEY", "priv123")
    monkeypatch.setenv("SAFE_VAR", "safe_val")

    runner = SandboxRunner(work_dir=str(tmp_path))
    cmd = (
        "python3 -c 'import os; "
        "print(list(k for k in os.environ.keys() if any(x in k for x in [\"SECRET\", \"PASSWORD\", \"TOKEN\", \"AUTH\", \"CREDENTIAL\", \"PRIVATE\", \"SAFE_VAR\"])))'"
    )
    res = runner.run_command(cmd)
    assert res["exit_code"] == 0
    assert "SAFE_VAR" in res["stdout"]
    assert "MY_CUSTOM_SECRET" not in res["stdout"]
    assert "USER_PASSWORD" not in res["stdout"]
    assert "OAUTH_TOKEN" not in res["stdout"]
    assert "AUTH_BEARER" not in res["stdout"]
    assert "CLIENT_CREDENTIAL" not in res["stdout"]
    assert "PRIVATE_KEY" not in res["stdout"]


def test_sandbox_env_override(tmp_path):
    runner = SandboxRunner(work_dir=str(tmp_path))
    res = runner.run_command(
        "python3 -c 'import os; print(os.environ.get(\"MY_CUSTOM_ENV\"))'",
        env_override={"MY_CUSTOM_ENV": "override_value"},
    )
    assert res["exit_code"] == 0
    assert "override_value" in res["stdout"]


def test_sandbox_nonzero_exit_code(tmp_path):
    runner = SandboxRunner(work_dir=str(tmp_path))
    res = runner.run_command("python3 -c 'import sys; sys.exit(42)'")
    assert res["exit_code"] == 42


def test_sandbox_invalid_work_dir(tmp_path):
    invalid_dir = str(tmp_path / "nonexistent_dir_12345")
    runner = SandboxRunner(work_dir=invalid_dir)
    res = runner.run_command("python3 -c 'print(1)'")
    assert res["exit_code"] == 1
    assert "Work directory does not exist" in res["stderr"] or "not found" in res["stderr"]


def test_sandbox_timeout_partial_output_preservation(tmp_path):
    runner = SandboxRunner(work_dir=str(tmp_path), timeout_sec=1)
    res = runner.run_command(
        "python3 -c 'import time, sys; print(\"PARTIAL_STDOUT_DATA\", flush=True); print(\"PARTIAL_STDERR_DATA\", file=sys.stderr, flush=True); time.sleep(5)'"
    )
    assert res["exit_code"] == 124
    assert "PARTIAL_STDOUT_DATA" in res["stdout"]
    assert "PARTIAL_STDERR_DATA" in res["stderr"]
    assert "TIMED_OUT" in res["stderr"]

