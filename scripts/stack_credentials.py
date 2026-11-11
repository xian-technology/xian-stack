#!/usr/bin/env python3
"""Private literal stack credentials shared by Python, shell and Make consumers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

CREDENTIAL_KEYS = ("XIAN_BDS_PASSWORD", "XIAN_POSTGRAPHILE_PASSWORD")
FORMAT_HEADER = "# xian-stack-credentials-v1"


def _literal_value(value: object) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("credential values must be strings without NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("credential values must be valid UTF-8") from exc
    return value


def _parse_value(raw: str, line_number: int, *, json_strings: bool) -> str:
    if json_strings:
        try:
            return _literal_value(json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid credential value on line {line_number}") from exc
    if raw.startswith(("'", '"')):
        if len(raw) < 2 or not raw.endswith(raw[0]):
            raise ValueError(f"invalid credential value on line {line_number}")
        raw = raw[1:-1]
    return _literal_value(raw)


def _parse_credentials(source: str) -> dict[str, str]:
    values: dict[str, str] = {}
    lines = source.splitlines()
    json_strings = _uses_json_values(lines)
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in CREDENTIAL_KEYS or key in values:
            raise ValueError(f"invalid credential assignment on line {line_number}")
        values[key] = _parse_value(value.strip(), line_number, json_strings=json_strings)
    return values


def _uses_json_values(lines: list[str]) -> bool:
    headers = [line for line in lines if line.startswith("# xian-stack-credentials-")]
    if not headers:
        return False
    if headers != [FORMAT_HEADER] or lines[0] != FORMAT_HEADER:
        raise ValueError("unsupported or misplaced credential format header")
    return True


def _open_regular(path: Path, flags: int) -> int:
    descriptor = os.open(path, flags | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"credential path must be a regular file: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_credentials(path: Path) -> dict[str, str]:
    try:
        descriptor = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return _parse_credentials(handle.read())


@contextmanager
def _credential_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = _open_regular(lock_path, os.O_RDWR | os.O_CREAT)
    with os.fdopen(descriptor, "a") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _write_credentials(path: Path, values: Mapping[str, str]) -> None:
    if path.is_symlink():
        raise ValueError(f"credential path must not be a symlink: {path}")
    if set(values) != set(CREDENTIAL_KEYS):
        raise ValueError("credential file must contain exactly the two stack password keys")
    lines = [FORMAT_HEADER, "# Values are JSON strings, never shell code."]
    for key in CREDENTIAL_KEYS:
        lines.append(f"{key}={json.dumps(_literal_value(values[key]))}")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_credentials(path: Path, values: Mapping[str, str]) -> None:
    with _credential_lock(path):
        _write_credentials(path, values)


def ensure_credentials(path: Path, environment: Mapping[str, str]) -> dict[str, str]:
    with _credential_lock(path):
        stored = read_credentials(path)
        values = {
            key: _literal_value(environment.get(key) or stored.get(key) or secrets.token_hex(24))
            for key in CREDENTIAL_KEYS
        }
        if not path.exists() or any(not stored.get(key) for key in CREDENTIAL_KEYS):
            _write_credentials(path, values)
        return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    path = arguments.path or Path(
        os.environ.get("XIAN_STACK_SECRETS_ENV")
        or Path(__file__).resolve().parents[1] / ".stack-secrets.env"
    )
    try:
        values = ensure_credentials(path.expanduser().absolute(), os.environ)
        if arguments.command:
            command = arguments.command
            if command[0] == "--":
                command = command[1:]
            if not command:
                raise ValueError("a command must follow --")
            os.execvpe(command[0], command, {**os.environ, **values})
        sys.stdout.buffer.write(b"\0".join(values[key].encode() for key in CREDENTIAL_KEYS) + b"\0")
    except (OSError, ValueError) as exc:
        print(f"xian-stack credentials: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
