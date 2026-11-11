#!/usr/bin/env python3
"""Translate Make's node settings into literal configure-node arguments."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from stack_credentials import ensure_credentials

BOOLEAN_OPTIONS = (
    ("XIAN_BDS_ENABLED", "--bds-enabled", "--no-bds-enabled"),
    ("XIAN_APP_METRICS_ENABLED", "--metrics-enabled", "--no-metrics-enabled"),
    ("XIAN_BDS_CATCHUP_ENABLED", "--bds-catchup-enabled", "--no-bds-catchup-enabled"),
)
FALSE_VALUES = {"0", "false", "FALSE", "False", "no", "NO", "No", "off", "OFF", "Off"}
VALUE_OPTIONS = (
    ("XIAN_APP_LOG_RETENTION_DAYS", "--app-log-retention-days"),
    ("XIAN_APP_LOG_MAX_FILE_MB", "--app-log-max-file-mb"),
    ("XIAN_APP_LOG_MAX_TOTAL_MB", "--app-log-max-total-mb"),
    ("XIAN_BLOCK_POLICY_MODE", "--block-policy-mode"),
    ("XIAN_BLOCK_POLICY_INTERVAL", "--block-policy-interval"),
    ("XIAN_TX_FEE_MODE", "--tx-fee-mode"),
    ("XIAN_FREE_TX_MAX_CHI", "--free-tx-max-chi"),
    ("XIAN_FREE_BLOCK_MAX_CHI", "--free-block-max-chi"),
    ("XIAN_APP_METRICS_LISTEN_HOST", "--metrics-host"),
    ("XIAN_APP_METRICS_PORT", "--metrics-port"),
    ("XIAN_APP_METRICS_BDS_REFRESH_SECONDS", "--metrics-bds-refresh-seconds"),
    ("XIAN_BDS_HOST", "--bds-host"),
    ("XIAN_BDS_PORT", "--bds-port"),
    ("XIAN_BDS_DATABASE", "--bds-database"),
    ("XIAN_BDS_USER", "--bds-user"),
    ("XIAN_BDS_POOL_MIN_SIZE", "--bds-pool-min-size"),
    ("XIAN_BDS_POOL_MAX_SIZE", "--bds-pool-max-size"),
    ("XIAN_BDS_STATEMENT_TIMEOUT_MS", "--bds-statement-timeout-ms"),
    ("XIAN_BDS_ACQUIRE_TIMEOUT_MS", "--bds-acquire-timeout-ms"),
    ("XIAN_BDS_APPLICATION_NAME", "--bds-application-name"),
    ("XIAN_BDS_QUEUE_MAX_SIZE", "--bds-queue-max-size"),
    ("XIAN_BDS_CATCHUP_POLL_SECONDS", "--bds-catchup-poll-seconds"),
    ("XIAN_BDS_SPOOL_WARN_ENTRIES", "--bds-spool-warn-entries"),
    ("XIAN_BDS_SPOOL_WARN_BYTES", "--bds-spool-warn-bytes"),
    ("XIAN_BDS_DISK_FREE_WARN_BYTES", "--bds-disk-free-warn-bytes"),
)
OPTIONAL_OPTIONS = (
    ("XIAN_BDS_DSN", "--bds-dsn"),
    ("XIAN_BDS_RPC_URL", "--bds-rpc-url"),
    ("XIAN_BDS_SPOOL_DIR", "--bds-spool-dir"),
)


def configuration_arguments(environment: Mapping[str, str]) -> list[str]:
    arguments = [
        disabled if environment[key] in FALSE_VALUES else enabled
        for key, enabled, disabled in BOOLEAN_OPTIONS
    ]
    for key, option in VALUE_OPTIONS:
        arguments.extend((option, environment[key]))
    # A valid password may itself begin with '-' and must remain one value.
    arguments.append("--bds-password=" + environment["XIAN_BDS_PASSWORD"])
    for key, option in OPTIONAL_OPTIONS:
        if environment.get(key, "").strip():
            arguments.extend((option, environment[key]))
    return arguments


def main() -> int:
    try:
        compose = shlex.split(os.environ.get("DOCKER_COMPOSE", "docker compose"))
        extra = shlex.split(os.environ.get("CONFIGURE_ARGS", ""))
        if not compose:
            raise ValueError("DOCKER_COMPOSE must name a command")
        path = Path(os.environ["XIAN_STACK_SECRETS_ENV"]).expanduser().absolute()
        environment = {**os.environ, **ensure_credentials(path, os.environ)}
        command = [
            *compose,
            "--profile",
            "integrated",
            "-f",
            "docker-compose-abci.yml",
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "xian-configure-node",
            "abci",
            *configuration_arguments(environment),
            *extra,
        ]
        return subprocess.run(command, env=environment, check=False).returncode
    except (OSError, ValueError, KeyError) as exc:
        print(f"xian-stack node configuration: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
