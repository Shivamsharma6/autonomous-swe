import logging
import os
import sys
import json
import time
from typing import Any, Dict, Optional

# Ensure logs directory exists
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "autoswe.log")

class StructuredFormatter(logging.Formatter):
    """Custom formatter with timestamps and structured formatting."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        log_obj = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "event_type"):
            log_obj["event_type"] = record.event_type
        if hasattr(record, "payload") and record.payload:
            log_obj["payload"] = record.payload
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)

def get_logger(name: str = "autoswe") -> logging.Logger:
    """Retrieve or create a logger configured with stdout and file handlers."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # Console Handler (Human-readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File Handler (Structured JSON format)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(StructuredFormatter())
    logger.addHandler(file_handler)

    return logger

logger = get_logger("autoswe")

def log_event(
    event_type: str,
    message: str,
    payload: Optional[Dict[str, Any]] = None,
    level: int = logging.INFO,
    logger_instance: Optional[logging.Logger] = None
) -> None:
    """Log structured events to file and console."""
    lg = logger_instance or logger
    extra = {"event_type": event_type, "payload": payload or {}}
    lg.log(level, f"[{event_type}] {message}", extra=extra)
