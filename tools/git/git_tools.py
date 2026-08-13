import os
import subprocess
from typing import Any, Dict


def git_status(workspace_path: str) -> Dict[str, Any]:
    try:
        res = subprocess.run(["git", "status", "--porcelain"], cwd=workspace_path, capture_output=True, text=True)
        return {"exit_code": res.returncode, "stdout": res.stdout, "stderr": res.stderr}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": str(e)}


def git_diff(workspace_path: str) -> Dict[str, Any]:
    try:
        res = subprocess.run(["git", "diff"], cwd=workspace_path, capture_output=True, text=True)
        return {"exit_code": res.returncode, "stdout": res.stdout, "stderr": res.stderr}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": str(e)}


def git_commit(workspace_path: str, message: str) -> Dict[str, Any]:
    try:
        subprocess.run(["git", "add", "."], cwd=workspace_path, check=True)
        res = subprocess.run(["git", "commit", "-m", message], cwd=workspace_path, capture_output=True, text=True)
        return {"exit_code": res.returncode, "stdout": res.stdout, "stderr": res.stderr}
    except Exception as e:
        return {"exit_code": 1, "stdout": "", "stderr": str(e)}
