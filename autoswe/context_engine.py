from typing import Any, Dict, List, Optional


class ContextBuilder:
    def __init__(self, token_budget: int = 4000):
        self.token_budget = token_budget

    def prune_text(self, text: str, max_chars: int = 2000) -> str:
        if len(text) <= max_chars:
            return text
        trunc_msg = "\n... [TRUNCATED FOR TOKEN BUDGET] ...\n"
        if max_chars <= len(trunc_msg):
            return text[:max_chars]
        avail = max_chars - len(trunc_msg)
        half = avail // 2
        rem = avail % 2
        return text[: half + rem] + trunc_msg + text[-half:]


class ContextEngine:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.builder = ContextBuilder()

    def assemble_context(
        self,
        task_request: str,
        repo_files: Optional[Dict[str, str]] = None,
        memory_notes: Optional[List[str]] = None,
        execution_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        sections = []

        # 1. Task Context
        sections.append(f"### Task Context\nGoal: {task_request}\n")

        # 2. Repository Context
        if repo_files:
            file_summaries = []
            for path, content in repo_files.items():
                pruned_content = self.builder.prune_text(content, max_chars=800)
                file_summaries.append(f"File: `{path}`:\n```\n{pruned_content}\n```")
            sections.append("### Repository Context\n" + "\n".join(file_summaries) + "\n")

        # 3. Memory Context
        if memory_notes:
            sections.append(
                "### Memory Context (Past Decisions & Rules)\n"
                + "\n".join(f"- {m}" for m in memory_notes)
                + "\n"
            )

        # 4. Execution Context (Critical for Debugging)
        if execution_context:
            exec_str = "### Execution Context (Diffs & Errors)\n"
            if "failed_command" in execution_context:
                exec_str += f"Failed Command: `{execution_context['failed_command']}`\n"
            if "stack_trace" in execution_context:
                exec_str += f"Stack Trace:\n```\n{self.builder.prune_text(execution_context['stack_trace'], 1000)}\n```\n"
            if "current_diff" in execution_context:
                exec_str += f"Current Diff:\n```diff\n{self.builder.prune_text(execution_context['current_diff'], 1000)}\n```\n"
            sections.append(exec_str)

        return "\n".join(sections)
