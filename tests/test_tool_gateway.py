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


def test_non_over_redaction_dict_keys():
    gateway = ToolGateway()
    payload = {
        "author": "John Doe",
        "keyword": "python",
        "token_count": 150,
        "api_key": "sk-1234567890abcdef1234567890abcdef",
        "secret": "supersecret",
    }
    redacted = gateway.redact_secrets(payload)
    assert redacted["author"] == "John Doe"
    assert redacted["keyword"] == "python"
    assert redacted["token_count"] == 150
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["secret"] == "[REDACTED]"


def test_error_message_secret_redaction():
    gateway = ToolGateway()

    # 1. executor_func returns ToolCallResult with secret in error
    def executor_with_error(req: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=req.call_id,
            tool_name=req.tool_name,
            output=None,
            error="Connection failed with token ghp_1234567890abcdef1234567890abcdef",
            is_success=False,
        )

    req1 = ToolCallRequest(call_id="c1", tool_name="test_tool", arguments={}, requested_by="user")
    res1 = gateway.execute_tool(req1, executor_func=executor_with_error)
    assert "ghp_" not in res1.error
    assert "[REDACTED]" in res1.error

    # 2. executor_func raises Exception containing a secret
    def executor_raises(req: ToolCallRequest) -> ToolCallResult:
        raise RuntimeError("Database error at postgres://user:secret123@localhost:5432/db")

    req2 = ToolCallRequest(call_id="c2", tool_name="test_tool", arguments={}, requested_by="user")
    res2 = gateway.execute_tool(req2, executor_func=executor_raises)
    assert "secret123@" not in res2.error
    assert "[REDACTED]" in res2.error


def test_pem_key_and_pat_redaction():
    gateway = ToolGateway()

    # GitHub fine-grained PAT
    pat_text = "Token: github_pat_11AAAAAAA0123456789abcdef_1234567890abcdef1234567890abcdef1234567890"
    redacted_pat = gateway.redact_secrets(pat_text)
    assert "github_pat_" not in redacted_pat
    assert "[REDACTED]" in redacted_pat

    # Slack token
    slack_text = "Slack token xoxb-1234567890-1234567890-abcdefghij"
    redacted_slack = gateway.redact_secrets(slack_text)
    assert "xoxb-" not in redacted_slack
    assert "[REDACTED]" in redacted_slack

    # PEM key
    pem_text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
    redacted_pem = gateway.redact_secrets(pem_text)
    assert "-----BEGIN RSA PRIVATE KEY-----" not in redacted_pem
    assert "[REDACTED]" in redacted_pem

    # DB connection URI
    db_text = "Connecting to redis://admin:password123@127.0.0.1:6379"
    redacted_db = gateway.redact_secrets(db_text)
    assert "password123@" not in redacted_db
    assert "[REDACTED]" in redacted_db


def test_rm_flag_permutations_risk_scoring():
    engine = RiskPolicyEngine()

    assert engine.evaluate_risk("run_command", {"command": "rm -fr /tmp/data"}) == RiskLevel.CRITICAL
    assert engine.evaluate_risk("run_command", {"command": "rm -r -f /tmp/data"}) == RiskLevel.CRITICAL
    assert engine.evaluate_risk("run_command", {"command": "rm -f -r /tmp/data"}) == RiskLevel.CRITICAL
    assert engine.evaluate_risk("run_command", {"command": "rm --recursive /tmp/data"}) == RiskLevel.CRITICAL
    assert engine.evaluate_risk("run_command", {"command": "rm -rf /tmp/data"}) == RiskLevel.CRITICAL

