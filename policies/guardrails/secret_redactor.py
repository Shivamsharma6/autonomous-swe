import re
from typing import Any, Dict, List, Union

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


def is_sensitive_key(key: str) -> bool:
    """Return True if key name indicates sensitive credential data."""
    k_lower = str(key).lower()
    if k_lower in SENSITIVE_KEY_EXACT:
        return True
    return any(pattern.match(k_lower) for pattern in SENSITIVE_KEY_PATTERNS)


class SecretRedactor:
    """Guardrail component for redacting secrets and sensitive data from outputs/logs."""

    def redact(self, data: Any) -> Any:
        """Recursively redact secrets from dictionaries, lists, and strings."""
        if isinstance(data, dict):
            redacted_dict = {}
            for k, v in data.items():
                if is_sensitive_key(k):
                    redacted_dict[k] = "[REDACTED]"
                else:
                    redacted_dict[k] = self.redact(v)
            return redacted_dict
        elif isinstance(data, list):
            return [self.redact(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(self.redact(item) for item in data)
        elif isinstance(data, str):
            res = data
            for pattern in SECRET_PATTERNS:
                res = pattern.sub("[REDACTED]", res)
            return res
        else:
            return data
