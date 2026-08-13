from typing import Any, Dict, Optional
from execution.sandbox.runner import SandboxRunner


def run_shell_command(workspace_path: str, command: str, timeout_sec: int = 30) -> Dict[str, Any]:
    runner = SandboxRunner(work_dir=workspace_path, timeout_sec=timeout_sec)
    return runner.run_command(command)
