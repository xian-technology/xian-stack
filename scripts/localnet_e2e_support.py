from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


class E2EError(RuntimeError):
    pass


def normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if value.__class__.__name__ == "ContractingDecimal":
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(key): normalize_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, tuple):
        return [normalize_value(item) for item in value]
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    return value


def parse_json_stdout(stdout: str, *, label: str) -> Any:
    stripped = stdout.strip()
    if not stripped:
        raise E2EError(f"{label} did not return JSON")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for start in reversed([index for index, char in enumerate(stripped) if char == "{"]):
        try:
            payload, end = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        if stripped[start + end :].strip():
            continue
        return payload
    raise E2EError(f"{label} did not return JSON: {stripped[-1000:]}")


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(serialized)


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
