import re
from typing import Any, Callable, Dict, List, Optional
from autoswe.models import RiskLevel, ToolCallRequest, ToolCallResult
from autoswe.storage import StorageEngine


SENSITIVE_KEY_EXACT = {
    "key",
    "api_key",
    "apikey",
    "secret",
    "password",
    "token",
    "auth",
    "authorization",
    "credential",
    "credentials",
    "private_key",
    "access_key",
    "auth_token",
    "access_token",
    "refresh_token",
    "secret_key",
    "client_secret",
    "aws_secret",
}

SENSITIVE_KEY_PATTERNS = [
    re.compile(r"^.*_(key|secret|password|token|credentials|auth)$", re.IGNORECASE),
    re.compile(r"^(api_key|secret|password|auth_token|access_key|private_key|credentials|token|auth)$", re.IGNORECASE),
]


def is_sensitive_key(key: str) -> bool:
    k_lower = str(key).lower()
    if k_lower in SENSITIVE_KEY_EXACT:
        return True
    return any(pattern.match(k_lower) for pattern in SENSITIVE_KEY_PATTERNS)


SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9_]{10,}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{30,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9_-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH|PRIVATE)(?:\s+PRIVATE)? KEY-----"),
    re.compile(r"(?:postgres|mysql|mongodb|redis)://[^:\s]+:[^@\s]+@"),
]

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
    def __init__(self, custom_rules: Optional[Dict[str, RiskLevel]] = None):
        self.custom_rules: Dict[str, RiskLevel] = custom_rules or {}

    def register_risk_rule(self, tool_name: str, risk_level: RiskLevel) -> None:
        self.custom_rules[tool_name] = risk_level

    def evaluate_risk(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> RiskLevel:
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


class ToolGateway:
    def __init__(
        self,
        storage_engine: Optional[StorageEngine] = None,
        risk_engine: Optional[RiskPolicyEngine] = None,
    ):
        self.storage_engine = storage_engine or StorageEngine()
        self.risk_engine = risk_engine or RiskPolicyEngine()

    def evaluate_risk(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> RiskLevel:
        return self.risk_engine.evaluate_risk(tool_name, arguments)

    def redact_secrets(self, data: Any) -> Any:
        if isinstance(data, dict):
            redacted_dict = {}
            for k, v in data.items():
                if is_sensitive_key(k):
                    redacted_dict[k] = "[REDACTED]"
                else:
                    redacted_dict[k] = self.redact_secrets(v)
            return redacted_dict
        elif isinstance(data, list):
            return [self.redact_secrets(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(self.redact_secrets(item) for item in data)
        elif isinstance(data, str):
            res = data
            for pattern in SECRET_PATTERNS:
                res = pattern.sub("[REDACTED]", res)
            return res
        else:
            return data

    def execute_tool(
        self,
        request: ToolCallRequest,
        executor_func: Optional[Callable[[ToolCallRequest], ToolCallResult]] = None,
        idempotency_key: Optional[str] = None,
    ) -> ToolCallResult:
        risk_level = self.evaluate_risk(request.tool_name, request.arguments)
        redacted_args = self.redact_secrets(request.arguments)

        # Log initiation audit event
        self.storage_engine.log_audit_event(
            event_type="TOOL_EXECUTION_REQUESTED",
            actor=request.requested_by,
            payload={
                "call_id": request.call_id,
                "tool_name": request.tool_name,
                "arguments": redacted_args,
                "risk_level": risk_level.value,
                "idempotency_key": idempotency_key,
            },
        )

        # Check idempotency record if key provided
        if idempotency_key:
            existing_record = self.storage_engine.get_idempotency_record(idempotency_key)
            if existing_record and existing_record.get("status") == "completed":
                cached_output = existing_record.get("result")
                self.storage_engine.log_audit_event(
                    event_type="TOOL_EXECUTION_CACHED",
                    actor=request.requested_by,
                    payload={
                        "call_id": request.call_id,
                        "tool_name": request.tool_name,
                        "idempotency_key": idempotency_key,
                    },
                )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    output=cached_output,
                    is_success=True,
                )

        # Execute tool if executor_func provided
        if executor_func:
            try:
                res = executor_func(request)
                redacted_output = self.redact_secrets(res.output)
                redacted_error = self.redact_secrets(res.error) if res.error is not None else None
                final_result = ToolCallResult(
                    call_id=res.call_id,
                    tool_name=res.tool_name,
                    output=redacted_output,
                    error=redacted_error,
                    is_success=res.is_success,
                )
            except Exception as e:
                final_result = ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    output=None,
                    error=self.redact_secrets(str(e)),
                    is_success=False,
                )

            if idempotency_key and final_result.is_success:
                self.storage_engine.save_idempotency_record(
                    key=idempotency_key,
                    result=final_result.output,
                    status="completed",
                )

            self.storage_engine.log_audit_event(
                event_type="TOOL_EXECUTION_COMPLETED" if final_result.is_success else "TOOL_EXECUTION_FAILED",
                actor=request.requested_by,
                payload={
                    "call_id": request.call_id,
                    "tool_name": request.tool_name,
                    "is_success": final_result.is_success,
                    "error": final_result.error,
                    "output": final_result.output,
                },
            )

            return final_result

        # Fallback if no executor function provided
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            output=None,
            is_success=True,
        )

