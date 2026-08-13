import os
import re
from typing import Any, Dict, List


def search_code(workspace_path: str, query: str) -> List[Dict[str, Any]]:
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("venv", "node_modules", "autoswe", "artifacts")]
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, workspace_path)
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for i, line in enumerate(lines, 1):
                    if pattern.search(line):
                        results.append({
                            "file": rel_path,
                            "line": i,
                            "content": line.strip(),
                        })
            except Exception:
                pass

    return results
