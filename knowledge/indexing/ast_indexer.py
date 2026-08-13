import ast
import os
from typing import Any, Dict, List, Optional


class ASTIndexer:
    """Parses python workspace files and extracts AST definitions (classes, functions, imports)."""

    def __init__(self, workspace_path: str):
        self.workspace_path = os.path.abspath(workspace_path)

    def index_workspace(self) -> Dict[str, Any]:
        index = {}
        if not os.path.exists(self.workspace_path):
            return index

        for root, dirs, files in os.walk(self.workspace_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("venv", "node_modules", "autoswe", "artifacts")]
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.workspace_path)
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        tree = ast.parse(content)
                        classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
                        functions = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
                        index[rel_path] = {"classes": classes, "functions": functions}
                    except Exception:
                        pass
        return index
