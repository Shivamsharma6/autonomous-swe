from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

import structlog

_SENSITIVE_KEYS = re.compile(
    r"(^|[_-])(authorization|cookie|password|secret|token|api[_-]?key|credential)($|[_-])",
    re.IGNORECASE,
)
_EMBEDDED_SECRET = re.compile(
    r"(?i)(bearer\s+|token=|password=|secret=|api[_-]?key=)[^\s,;]+"
)


def redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _EMBEDDED_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    return value


def redact_event(
    _: Any, __: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    return cast(dict[str, Any], redact(event_dict))


def configure_logging(*, json_logs: bool = True) -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=cast(
            Any,
            (
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                redact_event,
                renderer,
            ),
        ),
        wrapper_class=structlog.make_filtering_bound_logger(20),
        cache_logger_on_first_use=True,
    )


def get_structured_logger(name: str = "autoswe") -> Any:
    return structlog.get_logger(name)
