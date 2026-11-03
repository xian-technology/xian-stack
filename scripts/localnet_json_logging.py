"""Validate structured TRACE metadata without retaining transaction payloads."""

import json
import re
from datetime import datetime, timedelta

from localnet_e2e_support import E2EError

_FIELDS = {
    "stage",
    "tx_hash",
    "block_height",
    "block_hash",
    "tx_index",
    "sender",
    "contract",
    "function",
    "nonce",
    "status",
    "service",
    "pid",
    "receipt_bytes",
    "context",
}


def _require(condition, reason):
    if not condition:
        raise E2EError(f"JSON TRACE {reason}")


def _timestamp(envelope, record):
    try:
        timestamp = record["time"]["repr"]
        instant = datetime.fromisoformat(timestamp)
        _require(instant.utcoffset() == timedelta(0), "timestamp is not UTC")
        prefix = instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        _require(envelope["text"].startswith(prefix + " | "), "rendered UTC timestamp differs")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise E2EError("JSON TRACE timestamp is missing or invalid") from exc
    return timestamp


def _metadata(extra, record):
    _require(extra.keys() <= _FIELDS, "contains unexpected payload fields")
    for key, minimum in (
        ("block_height", 1),
        ("tx_index", 0),
        ("nonce", 0),
        ("pid", 1),
        ("receipt_bytes", 1),
    ):
        _require(type(extra.get(key)) is int and extra[key] >= minimum, f"invalid {key}")
    _require(
        extra.get("status") == 0 and type(extra["status"]) is int, "unexpected transaction status"
    )
    _require(extra.get("service") == "xian-abci", "invalid service context")
    process = record.get("process")
    _require(
        isinstance(process, dict) and process.get("id") == extra["pid"], "process context differs"
    )
    for key in ("block_hash", "sender"):
        _require(
            isinstance(extra.get(key), str) and re.fullmatch("[0-9a-fA-F]{64}", extra[key]),
            f"invalid {key}",
        )
    _require(
        extra.get("contract") == "currency" and extra.get("function") == "transfer",
        "wrong operation",
    )
    context = extra.get("context")
    _require(isinstance(context, str), "missing rendered context")
    for key in ("stage", "tx_hash", "receipt_bytes"):
        _require(f"{key}={extra[key]}" in context, f"missing rendered {key}")
    _require(
        record.get("message") == "Encoded finalized transaction receipt",
        "contains raw result message",
    )


def trace_record(line, submitted_hashes):
    """Return a correlated validated record, or None for unrelated/non-JSON lines."""
    try:
        envelope = json.loads(line)
        record = envelope["record"]
        extra = record["extra"]
        matches = (
            record["level"]["name"] == "TRACE"
            and extra["stage"] == "finalize_tx_result"
            and extra["tx_hash"] in submitted_hashes
        )
    except (ValueError, KeyError, TypeError):
        return None
    if not matches:
        return None
    timestamp = _timestamp(envelope, record)
    _metadata(extra, record)
    return {"timestamp": timestamp, **{key: extra[key] for key in sorted(_FIELDS - {"context"})}}
