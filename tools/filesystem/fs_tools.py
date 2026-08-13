import os
from typing import Any, Dict, List, Union


def write_file(workspace_path: str, rel_path: str, content: str) -> Dict[str, Any]:
    full_path = os.path.join(workspace_path, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": rel_path, "bytes_written": len(content)}


def read_file(workspace_path: str, rel_path: str) -> Dict[str, Any]:
    full_path = os.path.join(workspace_path, rel_path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"File not found: {rel_path}")
    with open(full_path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"path": rel_path, "content": content}


def list_dir(workspace_path: str, rel_path: str = ".") -> List[str]:
    full_path = os.path.join(workspace_path, rel_path)
    if not os.path.exists(full_path):
        return []
    return os.listdir(full_path)
