#!/usr/bin/env python3
"""Translate Make's node settings into literal configure-node arguments."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from genesis_fee_policy import genesis_fee_policy_arguments
from stack_credentials import ensure_credentials

BOOLEAN_OPTIONS = (
    ("XIAN_BDS_ENABLED", "--bds-enabled", "--no-bds-enabled"),
    ("XIAN_APP_METRICS_ENABLED", "--metrics-enabled", "--no-metrics-enabled"),
)
# docker-compose-abci.yml publishes XIAN_APP_METRICS_HOST:XIAN_APP_METRICS_PORT on the
# host to this container address, so the node always listens here; only the host side
# is configurable.
APP_METRICS_CONTAINER_HOST = "0.0.0.0"
APP_METRICS_CONTAINER_PORT = "9108"
# The integrated container runs CometBFT beside the node, so BDS catch-up reads
# the node's own RPC. All other [bds] settings keep xian-configure-node's
# defaults (the stack's Postgres service and its mounted password secret);
# change them with xian-configure-node flags in CONFIGURE_ARGS.
BDS_RPC_URL = "http://127.0.0.1:26657"
FALSE_VALUES = {"0", "false", "FALSE", "False", "no", "NO", "No", "off", "OFF", "Off"}
VALUE_OPTIONS = (
    ("XIAN_APP_LOG_RETENTION_DAYS", "--app-log-retention-days"),
    ("XIAN_APP_LOG_MAX_FILE_MB", "--app-log-max-file-mb"),
    ("XIAN_APP_LOG_MAX_TOTAL_MB", "--app-log-max-total-mb"),
    ("XIAN_BLOCK_POLICY_MODE", "--block-policy-mode"),
    ("XIAN_BLOCK_POLICY_INTERVAL", "--block-policy-interval"),
    ("XIAN_APP_METRICS_BDS_REFRESH_SECONDS", "--metrics-bds-refresh-seconds"),
)


def configuration_arguments(environment: Mapping[str, str]) -> list[str]:
    arguments = [
        disabled if environment[key] in FALSE_VALUES else enabled
        for key, enabled, disabled in BOOLEAN_OPTIONS
    ]
    for key, option in VALUE_OPTIONS:
        arguments.extend((option, environment[key]))
    arguments.extend(
        ("--metrics-host", APP_METRICS_CONTAINER_HOST, "--metrics-port", APP_METRICS_CONTAINER_PORT)
    )
    arguments.extend(("--bds-rpc-url", BDS_RPC_URL))
    return arguments


def _builds_bundle_genesis(arguments: list[str]) -> bool:
    return any(
        argument == "--genesis-bundle" or argument.startswith("--genesis-bundle=")
        for argument in arguments
    )


def main() -> int:
    try:
        compose = shlex.split(os.environ.get("DOCKER_COMPOSE", "docker compose"))
        extra = shlex.split(os.environ.get("CONFIGURE_ARGS", ""))
        if not compose:
            raise ValueError("DOCKER_COMPOSE must name a command")
        path = Path(os.environ["XIAN_STACK_SECRETS_ENV"]).expanduser().absolute()
        environment = {**os.environ, **ensure_credentials(path, os.environ)}
        fee_arguments = genesis_fee_policy_arguments(environment)
        if fee_arguments and not _builds_bundle_genesis(extra):
            raise ValueError(
                "XIAN_TX_FEE_MODE, XIAN_MAX_TX_CHI and XIAN_MAX_BLOCK_CHI set the "
                "genesis fee policy; pass --genesis-bundle in CONFIGURE_ARGS or unset them"
            )
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
            *fee_arguments,
            *extra,
        ]
        return subprocess.run(command, env=environment, check=False).returncode
    except (OSError, ValueError, KeyError) as exc:
        print(f"xian-stack node configuration: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
