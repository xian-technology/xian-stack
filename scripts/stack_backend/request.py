from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_REQUEST_SCHEMA_VERSION = 1


def read_backend_request(source: str) -> dict:
    raw_payload = sys.stdin.read() if source == "-" else Path(source).read_text(
        encoding="utf-8"
    )
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("backend request must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("backend request must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != BACKEND_REQUEST_SCHEMA_VERSION:
        raise ValueError(
            "backend request schema_version must be "
            f"{BACKEND_REQUEST_SCHEMA_VERSION}"
        )
    command = payload.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError("backend request command must be a non-empty string")
    options = payload.get("options", {})
    if not isinstance(options, dict):
        raise ValueError("backend request options must be an object")
    return {"command": command, "options": options}


def args_from_backend_request(
    parser: argparse.ArgumentParser,
    request: dict,
) -> argparse.Namespace:
    args = parser.parse_args([request["command"]])
    for key, value in request["options"].items():
        setattr(args, key, value)
    return args
