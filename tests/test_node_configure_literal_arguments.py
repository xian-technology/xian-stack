"""Make configuration transports credentials and operator options as argv data."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def configuration_probe(tmp_path):
    config_log = tmp_path / "configuration.json"
    configure = tmp_path / "xian-configure-node"
    configure.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from xian.cli.configure_node import build_parser\n"
        "parsed = build_parser().parse_args(sys.argv[1:])\n"
        "with open(os.environ['CONFIG_LOG'], 'w') as handle:\n"
        "    payload = {'args': sys.argv[1:], 'password': os.environ['XIAN_BDS_PASSWORD'],\n"
        "               'parsed_password': parsed.bds_password}\n"
        "    json.dump(payload, handle)\n"
    )
    configure.chmod(0o700)
    compose = tmp_path / "fake compose.py"
    compose.write_text(
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "entry = args[args.index('--entrypoint') + 1]\n"
        "remaining = args[args.index('abci') + 1:]\n"
        "if entry == '/bin/bash':\n"
        "    raise SystemExit(subprocess.call(['bash', '-c', remaining[1]]))\n"
        "assert entry == 'xian-configure-node', entry\n"
        "raise SystemExit(subprocess.call([os.environ['CONFIG_RECORDER'], *remaining]))\n"
    )
    secrets = tmp_path / "secrets.env"

    def run(password="plain-placeholder", source="file", overrides=None):
        secrets.write_text(
            "# xian-stack-credentials-v1\n"
            f"XIAN_BDS_PASSWORD={json.dumps(password if source == 'file' else 'file-fallback')}\n"
            'XIAN_POSTGRAPHILE_PASSWORD="different-placeholder"\n'
        )
        config_log.unlink(missing_ok=True)
        environment = {
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "XIAN_STACK_SECRETS_ENV": str(secrets),
            "XIAN_BDS_PASSWORD": password if source == "environment" else "",
            "XIAN_POSTGRAPHILE_PASSWORD": "",
            "CONFIG_LOG": str(config_log),
            "CONFIG_RECORDER": str(configure),
            "DOCKER_COMPOSE": shlex.join([sys.executable, str(compose)]),
            "CONFIGURE_ARGS": "--moniker placeholder",
            **(overrides or {}),
        }
        command = ["make", "--no-print-directory", "node-configure"]
        if source == "make":
            command.append(f"XIAN_BDS_PASSWORD={password}")
        result = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False
        )
        observed = json.loads(config_log.read_text()) if config_log.exists() else None
        return result, observed

    return run


def option_value(arguments, option):
    for index, argument in enumerate(arguments):
        if argument == option:
            return arguments[index + 1]
        if argument.startswith(option + "="):
            return argument.partition("=")[2]
    raise AssertionError(f"missing option {option}")


@pytest.mark.parametrize(
    "password",
    [
        "plain-placeholder",
        'apostrophe\'s "quoted" value\\end',
        "literal-$(printf changed)-`printf changed`-$HOME",
        " first line\nsecond line\n",
        "--starts-with-option",
    ],
)
@pytest.mark.parametrize("source", ["file", "environment", "make"])
def test_make_node_configure_keeps_credentials_and_options_literal(
    configuration_probe, password, source
):
    dsn = "postgresql://literal'host/db?option=$(printf changed)"
    moniker = "literal-$(printf changed)-`printf changed`"
    result, observed = configuration_probe(
        password,
        source,
        {"XIAN_BDS_DSN": dsn, "CONFIGURE_ARGS": shlex.join(["--moniker", moniker])},
    )
    assert result.returncode == 0, result.stderr
    # GNU Make itself trims assignment-leading whitespace before our code sees it.
    if source == "make":
        password = password.lstrip(" \t")
    assert option_value(observed["args"], "--bds-password") == password
    assert option_value(observed["args"], "--bds-dsn") == dsn
    assert option_value(observed["args"], "--moniker") == moniker
    assert observed["password"] == password
    assert observed["parsed_password"] == password
    assert password not in result.stdout


@pytest.mark.parametrize(
    "overrides, enabled, options",
    [
        (
            {"XIAN_BDS_ENABLED": "1"},
            ["--bds-enabled", "--metrics-enabled", "--bds-catchup-enabled"],
            {"--block-policy-mode": "periodic", "--block-policy-interval": "5s"},
        ),
        (
            {
                "XIAN_BDS_ENABLED": "0",
                "XIAN_APP_METRICS_ENABLED": "0",
                "XIAN_BDS_CATCHUP_ENABLED": "0",
            },
            ["--no-bds-enabled", "--no-metrics-enabled", "--no-bds-catchup-enabled"],
            {},
        ),
        (
            {"XIAN_BLOCK_POLICY_MODE": "on_demand", "XIAN_BLOCK_POLICY_INTERVAL": "0s"},
            [],
            {"--block-policy-mode": "on_demand", "--block-policy-interval": "0s"},
        ),
        (
            {
                "XIAN_TX_FEE_MODE": "free_metered",
                "XIAN_FREE_TX_MAX_CHI": "250000",
                "XIAN_FREE_BLOCK_MAX_CHI": "1000000",
                "CONFIGURE_ARGS": "--moniker m --genesis-bundle local --chain-id local-1",
            },
            [],
            {
                "--tx-fee-mode": "free_metered",
                "--free-tx-max-chi": "250000",
                "--free-block-max-chi": "1000000",
                "--genesis-bundle": "local",
            },
        ),
        (
            {"CONFIGURE_ARGS": "--block-policy-mode on_demand --moniker override"},
            [],
            {"--moniker": "override"},
        ),
    ],
)
def test_make_node_configure_preserves_runtime_flags_and_overrides(
    configuration_probe, overrides, enabled, options
):
    result, observed = configuration_probe(overrides=overrides)
    assert result.returncode == 0, result.stderr
    assert set(enabled) <= set(observed["args"])
    for option, expected in options.items():
        assert option_value(observed["args"], option) == expected


FEE_POLICY_ENVIRONMENT = {
    "XIAN_TX_FEE_MODE": "free_metered",
    "XIAN_FREE_TX_MAX_CHI": "250000",
    "XIAN_FREE_BLOCK_MAX_CHI": "1000000",
}
FEE_POLICY_OPTIONS = {"--tx-fee-mode", "--free-tx-max-chi", "--free-block-max-chi"}


def test_fee_policy_without_a_bundle_genesis_is_refused_before_configuration(
    configuration_probe,
):
    # The fee policy is genesis state; it cannot change an existing node's config.
    result, observed = configuration_probe(overrides=FEE_POLICY_ENVIRONMENT)

    assert result.returncode != 0
    assert "--genesis-bundle" in result.stderr
    assert observed is None


def test_unset_fee_policy_passes_no_fee_options(configuration_probe):
    result, observed = configuration_probe(overrides={name: "" for name in FEE_POLICY_ENVIRONMENT})

    assert result.returncode == 0, result.stderr
    assert not FEE_POLICY_OPTIONS & set(observed["args"])


def test_make_defaults_leave_the_fee_policy_to_genesis_defaults():
    printed = subprocess.run(
        ["make", "--no-print-directory", "print-env"],
        cwd=ROOT,
        env={key: value for key, value in os.environ.items() if key not in FEE_POLICY_ENVIRONMENT},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for name in FEE_POLICY_ENVIRONMENT:
        assert f"{name}=" in printed
