import re
from typing import Any

SENSITIVE_KEY_EXACT = {
    "key",
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "password",
    "passwd",
    "pwd",
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
    "aws_secret_access_key",
    "admin_token",
    "session_token",
    "jwt",
    "x-api-key",
    "connection_string",
    "dsn",
}

SENSITIVE_KEY_PATTERNS = [
    re.compile(r"^.*[-_](key|secret|password|passwd|token|credentials|auth|jwt)$", re.IGNORECASE),
    re.compile(
        r"^(api_?key|secret|password|passwd|auth_?token|access_?key|secret_?key"
        r"|private_?key|credentials|token|auth|admin_?token|client_?secret|dsn)$",
        re.IGNORECASE,
    ),
]

# A credential-looking assignment: the keyword may be separated from the
# separator by whitespace, and the separator may be `=`, `:` or `=>`, because
# the same secret appears in .env files, JSON, YAML, and shell exports. The
# earlier `=`-only, no-space form missed `AWS_SECRET_ACCESS_KEY=...`,
# `"api_key": "..."`, and `DB_PASSWORD : "..."`.
_ASSIGNMENT_KEYWORD = (
    r"(?:api_?keys?|secret(?:_access)?_?keys?|secrets?|passwords?|passwd|auth_?tokens?"
    r"|access_?tokens?|refresh_?tokens?|session_?tokens?|client_?secrets?"
    r"|private_?keys?|credentials?|admins?_?tokens?|aws_?secret_?access_?key"
    r"|x-?api-?keys?|dsn|connection_?strings?)"
)
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{10,}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{30,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{8,}"),
    re.compile(r"xox[baprs]-[a-zA-Z0-9_-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    # Whole PEM block: redacting only the header leaves the base64 body, which
    # is the part that actually reconstructs the key.
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"-----BEGIN (?:[A-Z0-9\s_-]+)?KEY-----"),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s\"']+:[^@\s\"']+@"),
re.compile(
        # The keyword may be prefixed (`DB_PASSWORD`, `AWS_SECRET_ACCESS_KEY`), so a
        # preceding underscore or dash is allowed while a preceding letter or digit
        # is not. An optional closing quote is allowed between the key and the
        # separator so JSON and YAML members are caught, not just .env lines.
        rf"(?i)(?<![A-Za-z0-9])[\"']?{_ASSIGNMENT_KEYWORD}[\"']?"
        rf"\s*[:=]{{1,2}}>?\s*[\"'`]?[^\s\"'`,;}}]{{8,}}"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]{10,}\b"),
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
