"""Bounded single-line records for stack-owned Python services."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

_CONTEXT_FIELDS = (
    "request_id",
    "method",
    "route",
    "status",
    "duration_ms",
    "error_type",
    "reason",
    "stack",
    "exit_code",
    "signal",
)


def safe_text(value: object, limit: int = 256) -> str:
    if type(value) is str:
        text = value[:limit]
    elif type(value) in (bool, float) or value is None:
        text = str(value)
    elif type(value) is int:
        text = str(value) if value.bit_length() <= 64 else "<large-int>"
    else:
        return "<unsupported>"
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def _message(record: logging.LogRecord) -> str:
    message = safe_text(record.msg, 1024)
    # Do not invoke arbitrary repr or unbounded %-width formatting on arguments.
    if type(record.args) is tuple:
        values = record.args[:8]
    elif type(record.args) is dict:
        values = [record.args[key] for key in islice(record.args, 8)]
    else:
        values = ()
    if values:
        message += " args=[" + ", ".join(safe_text(value, 128) for value in values) + "]"
    return message


class ServiceFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        raw = getattr(record, "context", {})
        context = raw if type(raw) is dict else {}
        fields = [f"{key}={safe_text(context[key])}" for key in _CONTEXT_FIELDS if key in context]
        if record.exc_info:
            fields.append(f"error_type={safe_text(record.exc_info[0].__name__)}")
            trace = record.exc_info[2]
            frames = []
            while trace is not None and len(frames) < 8:
                code = trace.tb_frame.f_code
                frames.append(
                    f"{safe_text(Path(code.co_filename).name, 64)}:"
                    f"{safe_text(code.co_name, 64)}:{trace.tb_lineno}"
                )
                trace = trace.tb_next
            fields.append("stack=" + ";".join(frames))
        return (
            f"{timestamp.replace('+00:00', 'Z')} | {record.levelname} | "
            f"{self.service}/{safe_text(record.name, 128)} | {_message(record)} | "
            + " ".join(fields)
        )


def configure_service_logging(service: str, level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(ServiceFormatter(service))
    logging.basicConfig(level=level, handlers=[handler], force=True)
