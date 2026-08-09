import pytest
from autoswe.models import RiskLevel, ToolCallRequest, ToolCallResult
from autoswe.storage import StorageEngine
from autoswe.tool_gateway import RiskPolicyEngine, ToolGateway


def test_risk_policy_engine_default_scoring():
    engine = RiskPolicyEngine()

    # LOW risk tools
    assert engine.evaluate_risk("read_file", {"path": "main.py"}) == RiskLevel.LOW
    assert engine.evaluate_risk("list_dir", {"path": "."}) == RiskLevel.LOW
    assert engine.evaluate_risk("view_file", {"path": "README.md"}) == RiskLevel.LOW

    # MEDIUM risk tools
    assert engine.evaluate_risk("write_file", {"path": "out.py"}) == RiskLevel.MEDIUM
    assert engine.evaluate_risk("edit_file", {"path": "out.py"}) == RiskLevel.MEDIUM

    # HIGH risk tools
    assert engine.evaluate_risk("run_command", {"command": "ls -la"}) == RiskLevel.HIGH
    assert engine.evaluate_risk("delete_file", {"path": "old.py"}) == RiskLevel.HIGH

    # CRITICAL risk commands / tools
    assert engine.evaluate_risk("run_command", {"command": "rm -rf /"}) == RiskLevel.CRITICAL
    assert engine.evaluate_risk("run_command", {"command": "drop database users"}) == RiskLevel.CRITICAL
    assert engine.evaluate_risk("purge_all_data", {}) == RiskLevel.CRITICAL


def test_risk_policy_engine_custom_rules():
    engine = RiskPolicyEngine()
    engine.register_risk_rule("deploy_app", RiskLevel.CRITICAL)
    assert engine.evaluate_risk("deploy_app", {}) == RiskLevel.CRITICAL

    engine.register_risk_rule("custom_read", RiskLevel.LOW)
    assert engine.evaluate_risk("custom_read", {}) == RiskLevel.LOW


def test_secret_redaction():
    gateway = ToolGateway()

    # Redact sensitive dict keys
    payload = {
        "api_key": "sk-1234567890abcdef1234567890abcdef",
        "user": "alice",
        "password": "SuperSecretPassword123!",
        "token": "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "config": {
            "aws_secret": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "normal_setting": "enabled"
        }
    }
    redacted = gateway.redact_secrets(payload)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["user"] == "alice"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["config"]["aws_secret"] == "[REDACTED]"
    assert redacted["config"]["normal_setting"] == "enabled"


def test_secret_redaction_pattern_matching_in_text():
    gateway = ToolGateway()

    # Strings containing tokens or secret patterns
    text = "Connecting with token ghp_1234567890abcdef1234567890abcdef and Key sk-abcdef1234567890abcdef1234"
    redacted_text = gateway.redact_secrets(text)

    assert "ghp_" not in redacted_text or "[REDACTED]" in redacted_text
    assert "sk-" not in redacted_text or "[REDACTED]" in redacted_text
    assert "[REDACTED]" in redacted_text


def test_idempotency_key_execution_check(tmp_path):
    db_file = str(tmp_path / "test_gw.db")
    storage = StorageEngine(db_path=db_file, storage_dir=str(tmp_path / "artifacts"))
    gateway = ToolGateway(storage_engine=storage)

    execution_count = 0

    def executor(request: ToolCallRequest) -> ToolCallResult:
        nonlocal execution_count
        execution_count += 1
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            output={"status": "created", "count": execution_count},
            is_success=True
        )

    req = ToolCallRequest(
        call_id="call-1",
        tool_name="create_resource",
        arguments={"name": "test_item"},
        requested_by="agent-1"
    )

    # First call with idempotency key
    res1 = gateway.execute_tool(req, executor_func=executor, idempotency_key="idempotent-key-100")
    assert res1.is_success
    assert res1.output["count"] == 1
    assert execution_count == 1

    # Second call with SAME idempotency key
    res2 = gateway.execute_tool(req, executor_func=executor, idempotency_key="idempotent-key-100")
    assert res2.is_success
    assert res2.output["count"] == 1  # returned cached result
    assert execution_count == 1  # executor was NOT called again!


def test_audit_logging(tmp_path):
    db_file = str(tmp_path / "test_gw_audit.db")
    storage = StorageEngine(db_path=db_file, storage_dir=str(tmp_path / "artifacts"))
    gateway = ToolGateway(storage_engine=storage)

    req = ToolCallRequest(
        call_id="call-audit-1",
        tool_name="read_file",
        arguments={"path": "secret.txt", "api_key": "secret123"},
        requested_by="agent-test"
    )

    def executor(request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            output="File content here",
            is_success=True
        )

    gateway.execute_tool(req, executor_func=executor)

    # Verify audit logs in storage
    with storage._get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs ORDER BY id ASC")
        rows = cursor.fetchall()

    assert len(rows) >= 1
    # Check that secrets in arguments were redacted in audit log payload
    payload_str = str([dict(row) for row in rows])
    assert "secret123" not in payload_str
    assert "[REDACTED]" in payload_str
