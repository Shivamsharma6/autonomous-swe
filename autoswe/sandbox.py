import os
import subprocess
import time
from typing import Any, Dict, Optional

SENSITIVE_ENV_KEYS = {
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SECRET_KEY",
    "API_KEY",
    "PASSWORD",
    "TOKEN",
    "AUTH_TOKEN",
    "DATABASE_URL",
}


class SandboxRunner:
    """Sandboxed command runner with timeout enforcement and environment variable isolation."""

    def __init__(self, work_dir: str, timeout_sec: int = 30):
        self.work_dir = os.path.abspath(work_dir)
        self.timeout_sec = timeout_sec

    def run_command(
        self, command_str: str, env_override: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        env = os.environ.copy()
        for key in SENSITIVE_ENV_KEYS:
            env.pop(key, None)

        if env_override:
            env.update(env_override)

        start_time = time.time()
        try:
            proc = subprocess.run(
                command_str,
                shell=True,
                cwd=self.work_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
            duration_ms = (time.time() - start_time) * 1000.0
            return {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "duration_ms": duration_ms,
            }
        except subprocess.TimeoutExpired:
            duration_ms = (time.time() - start_time) * 1000.0
            return {
                "exit_code": 124,
                "stdout": "",
                "stderr": f"TIMED_OUT after {self.timeout_sec}s",
                "duration_ms": duration_ms,
            }
