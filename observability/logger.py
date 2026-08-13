from __future__ import annotations

import logging
from typing import Any

from observability.logging import get_structured_logger, redact


def get_logger(name: str = "autonomous_swe") -> logging.Logger:
    """Compatibility standard-library logger configured for container stdout."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


logger = get_logger()


def log_event(
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    level: int = logging.INFO,
    logger_instance: logging.Logger | None = None,
) -> None:
    """Emit a redacted structured event without writing host-side log files."""
    cleaned = redact(payload or {})
    if logger_instance is not None:
        logger_instance.log(level, "%s %s %s", event_type, message, cleaned)
        return
    if level >= logging.ERROR:
        method = "error"
    elif level >= logging.WARNING:
        method = "warning"
    else:
        method = "info"
    getattr(get_structured_logger(), method)(event_type, message=message, **cleaned)
