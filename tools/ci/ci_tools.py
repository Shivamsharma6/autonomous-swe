import os
from typing import Any, Dict
from execution.sandbox.runner import SandboxRunner


def run_pytest_suite(workspace_path: str, target: Optional[str] = None) -> Dict[str, Any]:
    runner = SandboxRunner(work_dir=workspace_path)
    target_arg = target or workspace_path
    cmd = f"python3 -m pytest {target_arg} -c /dev/null -o rootdir={workspace_path}"
    env_override = {"PYTHONPATH": f"{workspace_path}:{os.environ.get('PYTHONPATH', '')}"}
    return runner.run_command(cmd, env_override=env_override)
