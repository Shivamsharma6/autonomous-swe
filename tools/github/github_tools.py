from typing import Any, Dict, Optional


def create_pull_request(repo: str, title: str, body: str, head_branch: str, base_branch: str = "main") -> Dict[str, Any]:
    """Stub for creating a GitHub pull request."""
    return {
        "status": "created",
        "pr_url": f"https://github.com/{repo}/pull/101",
        "title": title,
        "head": head_branch,
        "base": base_branch,
    }


def create_issue(repo: str, title: str, body: str) -> Dict[str, Any]:
    """Stub for creating a GitHub issue."""
    return {
        "status": "created",
        "issue_url": f"https://github.com/{repo}/issues/42",
        "title": title,
    }
