from typing import Any, Callable, Dict, Optional
from pydantic import BaseModel, Field
from policies.risk.policy_engine import RiskLevel, RiskPolicyEngine
from policies.guardrails.secret_redactor import SecretRedactor
from knowledge.memory.storage import StorageEngine


class ToolCallRequest(BaseModel):
    """Request to execute a tool call."""

    call_id: str
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    requested_by: str


class ToolCallResult(BaseModel):
    """Result from a tool call execution."""

    call_id: str
    tool_name: str
    output: Any = None
    error: Optional[str] = None
    is_success: bool = True


class ToolGateway:
    """Central gateway for tool execution, risk assessment, secret redaction, and idempotency caching."""

    def __init__(
        self,
        storage_engine: Optional[StorageEngine] = None,
        risk_engine: Optional[RiskPolicyEngine] = None,
    ):
        self.storage_engine = storage_engine or StorageEngine()
        self.risk_engine = risk_engine or RiskPolicyEngine()
        self.redactor = SecretRedactor()

    def evaluate_risk(
        self, tool_name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> RiskLevel:
        return self.risk_engine.evaluate_risk(tool_name, arguments)

    def redact_secrets(self, data: Any) -> Any:
        return self.redactor.redact(data)

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
