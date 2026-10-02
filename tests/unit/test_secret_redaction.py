"""Secrets must not survive the round trip through artifacts or the API.

The exfiltration chain these tests close: a prompt injection in an imported
repository persuades an agent to `read_file(".env")`, the tool result is
persisted, the model copies the value into its summary, the summary is written
to a content-addressed artifact and a handoff message, and both are served back
over the API. Redacting only the tool result leaves the rest of the chain open.
"""

from __future__ import annotations

import pytest

from policies.guardrails.secret_redactor import SecretRedactor, is_sensitive_key

REDACTOR = SecretRedactor()

LEAKY = [
    ("env assignment", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG"),
    ("underscore prefixed", "DB_" + "PASSWORD=hunter2hunter"),
    ("json member", '{"api_key": "abcd1234efgh5678"}'),
    ("json with space", '{"api_key" : "abcd1234efgh5678"}'),
    ("yaml member", "client_secret: abcdef123456"),
    ("spaced equals", "password = supersecretvalue"),
    ("connection string", "postgresql://autoswe:hunter2@postgres:5432/autoswe"),
    ("bearer header", "Authorization: Bearer abcdefgh12345678"),
    ("aws access key id", "AKIAIOSFODNN7EXAMPLE"),
    ("github token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"),
    ("openai key", "sk-proj-abcdefghijklmnopqrstuvwxyz012345"),
    ("slack token", "xoxb-1234567890-abcdefghij"),
    ("google api key", "AIzaSyA1234567890abcdefghijklmnopqrstuvw"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"),
    ("pem block", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabcdef\n-----END RSA PRIVATE KEY-----"),
]

BENIGN = [
    "normal prose with no secrets at all",
    "value = 3",
    "def read_file(path): return path",
    "see the acceptance criteria below",
    "the notasecret keyword is documented",
    "import os; os.environ[\"HOME\"]",
    "Run the suite with pytest -q and report the result.",
    "summarize the repository layout and its entry points",
]


@pytest.mark.parametrize("label, payload", LEAKY, ids=[case[0] for case in LEAKY])
def test_credential_shaped_input_is_redacted(label: str, payload: str) -> None:
    redacted = REDACTOR.redact(payload)

    assert "[REDACTED]" in redacted, f"{label} leaked: {redacted!r}"


@pytest.mark.parametrize("payload", BENIGN)
def test_ordinary_content_is_preserved(payload: str) -> None:
    assert REDACTOR.redact(payload) == payload


def test_pem_body_is_removed_not_just_the_header() -> None:
    payload = (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHcCAQEEIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\n"
        "-----END EC PRIVATE KEY-----"
    )

    redacted = REDACTOR.redact(payload)

    assert "MHcCAQEEIBBBB" not in redacted


def test_nested_structures_are_redacted() -> None:
    payload = {
        "path": ".env",
        "content": "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG",
        "nested": [{"password": "hunter2hunter"}, {"note": "fine"}],
    }

    redacted = REDACTOR.redact(payload)

    assert "wJalrXUtnFEMI" not in str(redacted)
    assert redacted["nested"][0]["pass" + "word"] == "[REDACTED]"
    assert redacted["nested"][1]["note"] == "fine"


@pytest.mark.parametrize(
    "key",
    ["api_key", "PASSWORD", "aws_secret_access_key", "x-api-key", "db_password", "credentials"],
)
def test_sensitive_key_names_are_recognised(key: str) -> None:
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize("key", ["path", "summary", "goal", "count", "read_file"])
def test_ordinary_key_names_are_not_treated_as_secrets(key: str) -> None:
    assert is_sensitive_key(key) is False


def test_redaction_is_idempotent() -> None:
    once = REDACTOR.redact("API_KEY=abcdefgh12345678")

    assert REDACTOR.redact(once) == once
