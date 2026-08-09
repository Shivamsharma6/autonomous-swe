import os
import signal
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

SENSITIVE_TERMS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "AUTH",
    "CREDENTIAL",
    "PRIVATE",
)


def _to_str(val: Optional[Any]) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


class SandboxRunner:
    """Sandboxed command runner with timeout enforcement and environment variable isolation."""

    def __init__(self, work_dir: str, timeout_sec: int = 30):
        self.work_dir = os.path.abspath(work_dir)
        self.timeout_sec = timeout_sec

    def run_command(
        self, command_str: str, env_override: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        start_time = time.time()

        if not os.path.exists(self.work_dir) or not os.path.isdir(self.work_dir):
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": f"Work directory does not exist: {self.work_dir}",
                "duration_ms": (time.time() - start_time) * 1000.0,
            }

        env = os.environ.copy()
        for key in list(env.keys()):
            key_upper = key.upper()
            if any(term in key_upper for term in SENSITIVE_TERMS) or key in SENSITIVE_ENV_KEYS:
                env.pop(key, None)

        if env_override:
            env.update(env_override)

        try:
            proc = subprocess.Popen(
                command_str,
                shell=True,
                cwd=self.work_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout_sec)
                duration_ms = (time.time() - start_time) * 1000.0
                return {
                    "exit_code": proc.returncode,
                    "stdout": stdout or "",
                    "stderr": stderr or "",
                    "duration_ms": duration_ms,
                }
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                out_extra, err_extra = proc.communicate()
                duration_ms = (time.time() - start_time) * 1000.0
                stdout = _to_str(out_extra or exc.stdout)
                stderr = _to_str(err_extra or exc.stderr)
                timed_out_msg = f"TIMED_OUT after {self.timeout_sec}s"
                if timed_out_msg not in stderr:
                    stderr = f"{stderr}\n{timed_out_msg}".strip() if stderr else timed_out_msg
                return {
                    "exit_code": 124,
                    "stdout": stdout,
                    "stderr": stderr,
                    "duration_ms": duration_ms,
                }
        except FileNotFoundError as exc:
            duration_ms = (time.time() - start_time) * 1000.0
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": f"Work directory not found: {self.work_dir} ({exc})",
                "duration_ms": duration_ms,
            }

