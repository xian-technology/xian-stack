#!/usr/bin/env python3
"""Private literal stack credentials shared by Python, shell and Make consumers.

The credentials file holds the PostGraphile password. The BDS password lives
only in its own owner-only file (XIAN_BDS_PASSWORD_FILE, by default
``.stack-bds-password`` beside the credentials file); Compose mounts it as the
secret /run/secrets/xian_bds_password for Postgres and the node. Consumers
receive the file's path, never the password.
"""

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

CREDENTIAL_KEYS = ("XIAN_POSTGRAPHILE_PASSWORD",)
FORMAT_HEADER = "# xian-stack-credentials-v1"
BDS_PASSWORD_FILE_KEY = "XIAN_BDS_PASSWORD_FILE"
BDS_PASSWORD_FILE_NAME = ".stack-bds-password"
BDS_DATABASE_USER = "xian"
# BDS node settings live only in the node's xian.toml [bds] table and its
# password only in the BDS password file. These variables were a second,
# silently merged source; setting one now fails instead of being ignored.
REMOVED_BDS_VARIABLES = tuple(
    f"XIAN_BDS_{name}"
    for name in (
        "DSN",
        "HOST",
        "PORT",
        "DATABASE",
        "USER",
        "PASSWORD",
        "POOL_MIN_SIZE",
        "POOL_MAX_SIZE",
        "STATEMENT_TIMEOUT_MS",
        "ACQUIRE_TIMEOUT_MS",
        "APPLICATION_NAME",
        "QUEUE_MAX_SIZE",
        "CATCHUP_ENABLED",
        "CATCHUP_POLL_SECONDS",
        "RPC_URL",
        "SPOOL_DIR",
        "SPOOL_WARN_ENTRIES",
        "SPOOL_WARN_BYTES",
        "DISK_FREE_WARN_BYTES",
    )
)


def reject_removed_bds_variables(environment: Mapping[str, str]) -> None:
    removed = [name for name in REMOVED_BDS_VARIABLES if name in environment]
    if removed:
        raise ValueError(
            f"no longer read: {', '.join(removed)}. BDS node settings live only in the "
            "node's xian.toml [bds] table (change them with xian-configure-node flags in "
            "CONFIGURE_ARGS or the xian-cli profile) and the BDS password only in the "
            f"{BDS_PASSWORD_FILE_KEY} file; unset these variables"
        )


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
        if key == "XIAN_BDS_PASSWORD":
            raise ValueError(
                f"XIAN_BDS_PASSWORD on line {line_number} is no longer read from the "
                "credentials file: the BDS password lives only in the "
                f"{BDS_PASSWORD_FILE_KEY} file (default {BDS_PASSWORD_FILE_NAME} beside "
                "this file, mode 0600); move the value there and delete the line"
            )
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
        raise ValueError(f"credential file must contain exactly {', '.join(CREDENTIAL_KEYS)}")
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


def bds_password_path(path: Path, environment: Mapping[str, str]) -> Path:
    explicit = environment.get(BDS_PASSWORD_FILE_KEY)
    if explicit:
        return Path(explicit).expanduser().absolute()
    return path.with_name(BDS_PASSWORD_FILE_NAME)


def read_bds_password(path: Path) -> str | None:
    """The password in ``path`` (None when it does not exist). Trailing
    newlines are not part of it, as for Postgres' POSTGRES_PASSWORD_FILE."""
    try:
        descriptor = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        if stat.S_IMODE(os.fstat(handle.fileno()).st_mode) & 0o077:
            raise ValueError(f"BDS password file must be readable only by its owner: {path}")
        password = handle.read().rstrip("\n")
    if not password:
        raise ValueError(f"BDS password file is empty: {path}")
    return password


def write_bds_password_file(path: Path) -> None:
    """Atomically write a fresh random BDS password, owner-only."""
    password = secrets.token_hex(24)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(password)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def bds_password_errors(bds_password: str, postgraphile_password: str) -> list[str]:
    errors = []
    if bds_password == BDS_DATABASE_USER:
        errors.append("the BDS password cannot use the weak default value 'xian'")
    if postgraphile_password == bds_password:
        errors.append("XIAN_POSTGRAPHILE_PASSWORD must differ from the BDS password")
    return errors


def ensure_credentials(path: Path, environment: Mapping[str, str]) -> dict[str, str]:
    """The PostGraphile password and the BDS password file's path, creating
    whichever is missing."""
    reject_removed_bds_variables(environment)
    password_path = bds_password_path(path, environment)
    with _credential_lock(path):
        stored = read_credentials(path)
        values = {
            key: _literal_value(environment.get(key) or stored.get(key) or secrets.token_hex(24))
            for key in CREDENTIAL_KEYS
        }
        bds_password = read_bds_password(password_path)
        errors = bds_password_errors(bds_password or "", values["XIAN_POSTGRAPHILE_PASSWORD"])
        if errors:
            raise ValueError("; ".join(errors))
        if not path.exists() or any(not stored.get(key) for key in CREDENTIAL_KEYS):
            _write_credentials(path, values)
        if bds_password is None:
            password_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_bds_password_file(password_path)
        return {**values, BDS_PASSWORD_FILE_KEY: str(password_path)}


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
        printed = (*CREDENTIAL_KEYS, BDS_PASSWORD_FILE_KEY)
        sys.stdout.buffer.write(b"\0".join(values[key].encode() for key in printed) + b"\0")
    except (OSError, ValueError) as exc:
        print(f"xian-stack credentials: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
