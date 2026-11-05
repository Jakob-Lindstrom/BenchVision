"""Structured logging setup writing strictly inside the configured data folder."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(data_dir: Path, level: str = "INFO") -> Path:
    """Configure root/app logger with console and rotating JSON file handler in <data_dir>/logs."""
    logs_dir = Path(data_dir).resolve() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "benchvision.log"

    logger = logging.getLogger("benchvision")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(StructuredJsonFormatter())
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(console_handler)

    return log_file


def read_recent_logs(data_dir: Path, max_lines: int = 200) -> list[dict]:
    """Read and parse the most recent structured log lines from <data_dir>/logs/benchvision.log."""
    log_file = Path(data_dir).resolve() / "logs" / "benchvision.log"
    if not log_file.exists():
        return []
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    entries: list[dict] = []
    for raw in lines[-max_lines:]:
        try:
            entries.append(json.loads(raw))
        except (ValueError, TypeError):
            entries.append({"timestamp": "", "level": "INFO", "message": raw})
    return entries
