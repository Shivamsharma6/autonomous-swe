import re
from enum import Enum
from typing import Any, Dict, Optional


class RiskLevel(str, Enum):
    """Risk level classification for tools and operations."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


CRITICAL_ARG_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r|--recursive)", re.IGNORECASE),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    re.compile(r"\btruncate\s+table\b", re.IGNORECASE),
    re.compile(r"\bpurge_all\b", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
]


class RiskPolicyEngine:
    """Evaluates risk levels of tool executions and system actions."""

    def __init__(self, custom_rules: Optional[Dict[str, RiskLevel]] = None):
        self.custom_rules: Dict[str, RiskLevel] = custom_rules or {}

    def register_risk_rule(self, tool_name: str, risk_level: RiskLevel) -> None:
        """Register a custom risk classification rule for a tool."""
        self.custom_rules[tool_name] = risk_level

    def evaluate_risk(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> RiskLevel:
        """Evaluate the risk level of executing a tool with given arguments."""
        # 1. Custom rule check
        if tool_name in self.custom_rules:
            return self.custom_rules[tool_name]

        args_str = str(arguments or {})

        # 2. Check for CRITICAL destructive patterns in arguments or tool name
        for pattern in CRITICAL_ARG_PATTERNS:
            if pattern.search(args_str):
                return RiskLevel.CRITICAL

        if any(keyword in tool_name.lower() for keyword in ["purge", "destroy_all", "drop_db", "delete_database"]):
            return RiskLevel.CRITICAL

        # 3. High risk tools / commands
        if tool_name in ("run_command", "bash", "shell", "exec") or tool_name.startswith(("delete_", "remove_", "kill_", "drop_")):
            return RiskLevel.HIGH

        # 4. Medium risk tools (state modifying)
        if tool_name.startswith(("write_", "edit_", "create_", "update_", "modify_", "save_", "post_", "put_")):
            return RiskLevel.MEDIUM

        # 5. Low risk tools (read-only)
        if tool_name.startswith(("read_", "list_", "get_", "view_", "search_", "fetch_", "describe_", "check_", "inspect_")):
            return RiskLevel.LOW

        return RiskLevel.MEDIUM
