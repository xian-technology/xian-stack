from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_REQUEST_SCHEMA_VERSION = 1


def read_backend_request(source: str) -> dict:
    raw_payload = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("backend request must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("backend request must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != BACKEND_REQUEST_SCHEMA_VERSION:
        raise ValueError(f"backend request schema_version must be {BACKEND_REQUEST_SCHEMA_VERSION}")
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
    option_actions = _option_actions_for_command(parser, request["command"])
    args = parser.parse_args([request["command"]])
    for key, value in request["options"].items():
        action = option_actions.get(key)
        if action is None:
            raise ValueError(
                f"backend request option is not supported for {request['command']}: {key}"
            )
        if value is None:
            continue
        setattr(args, key, _coerce_backend_option(action, value))
    return args


def _option_actions_for_command(
    parser: argparse.ArgumentParser,
    command: str,
) -> dict[str, argparse.Action]:
    command_parser = _command_parser(parser, command)
    return {
        action.dest: action
        for action in command_parser._actions
        if action.option_strings and action.dest != argparse.SUPPRESS
    }


def _command_parser(
    parser: argparse.ArgumentParser,
    command: str,
) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            command_parser = action.choices.get(command)
            if command_parser is not None:
                return command_parser
    raise ValueError(f"backend request command is not supported: {command}")


def _coerce_backend_option(action: argparse.Action, value: object) -> object:
    if isinstance(action, argparse.BooleanOptionalAction):
        if not isinstance(value, bool):
            raise ValueError(f"{action.dest} must be a boolean")
        return value
    if action.__class__.__name__ == "_AppendAction":
        if not isinstance(value, list):
            raise ValueError(f"{action.dest} must be a list")
        return [_coerce_backend_scalar(action, item) for item in value]
    if isinstance(value, (list, dict, tuple)):
        raise ValueError(f"{action.dest} must be a scalar value")
    return _coerce_backend_scalar(action, value)


def _coerce_backend_scalar(action: argparse.Action, value: object) -> object:
    if isinstance(value, bool):
        raise ValueError(f"{action.dest} must not be a boolean")
    if action.type is None:
        if not isinstance(value, str):
            raise ValueError(f"{action.dest} must be a string")
        coerced = value
    else:
        try:
            coerced = action.type(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{action.dest} must be {action.type.__name__}") from exc
    choices = action.choices
    if choices is not None and coerced not in choices:
        choice_values = sorted(str(choice) for choice in choices)
        raise ValueError(f"{action.dest} must be one of {choice_values}")
    return coerced
