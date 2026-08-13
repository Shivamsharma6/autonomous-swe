from typing import Any, Dict, List, Optional
from policies.risk.policy_engine import RiskLevel


class PermissionChecker:
    """Evaluates actor permissions and tool authorization policies."""

    def __init__(self, allowed_roles: Optional[List[str]] = None):
        self.allowed_roles = allowed_roles or ["Architect", "Coder", "Tester", "Reviewer", "Debugger", "Researcher"]

    def can_execute_tool(self, actor_role: str, tool_name: str, risk_level: RiskLevel) -> bool:
        """Check if actor role is permitted to execute tool at specified risk level."""
        if actor_role not in self.allowed_roles:
            return False
        if risk_level == RiskLevel.CRITICAL and actor_role not in ("Architect", "Reviewer"):
            return False
        return True
