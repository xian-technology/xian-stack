"""Make configuration transports operator options as argv data.

The BDS password is never an argument or environment value: configure-node
records only the secret's path (its default, /run/secrets/xian_bds_password).
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]


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
        "    stack = ('XIAN_BDS_ENABLED', 'XIAN_BDS_DATA_DIR', 'XIAN_BDS_PASSWORD_FILE')\n"
        "    bds = sorted(k for k in os.environ if k.startswith('XIAN_BDS_')\n"
        "                 and k not in stack and not k.startswith('XIAN_BDS_SNAPSHOT'))\n"
        "    payload = {'args': sys.argv[1:], 'bds_environment': bds,\n"
        "               'password_file': parsed.bds_password_file,\n"
        "               'rpc_url': parsed.bds_rpc_url}\n"
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
    bds_password = tmp_path / ".stack-bds-password"

    def run(password="plain-placeholder", overrides=None, make_arguments=()):
        secrets.write_text(
            '# xian-stack-credentials-v1\nXIAN_POSTGRAPHILE_PASSWORD="different-placeholder"\n'
        )
        bds_password.write_text(password)
        bds_password.chmod(0o600)
        config_log.unlink(missing_ok=True)
        environment = {
            **{key: value for key, value in os.environ.items() if not key.startswith("XIAN_BDS_")},
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "XIAN_STACK_SECRETS_ENV": str(secrets),
            "XIAN_POSTGRAPHILE_PASSWORD": "",
            "CONFIG_LOG": str(config_log),
            "CONFIG_RECORDER": str(configure),
            "DOCKER_COMPOSE": shlex.join([sys.executable, str(compose)]),
            "CONFIGURE_ARGS": "--moniker placeholder",
            **(overrides or {}),
        }
        command = ["make", "--no-print-directory", "node-configure", *make_arguments]
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
        "--starts-with-option",
    ],
)
def test_make_node_configure_never_handles_the_bds_password(configuration_probe, password):
    moniker = "literal-$(printf changed)-`printf changed`"
    result, observed = configuration_probe(
        password, {"CONFIGURE_ARGS": shlex.join(["--moniker", moniker])}
    )
    assert result.returncode == 0, result.stderr
    assert password not in observed["args"]
    assert not [arg for arg in observed["args"] if arg.startswith("--bds-password")]
    assert observed["bds_environment"] == []
    # xian.toml names the mounted secret; the stack's node reaches its own RPC.
    assert observed["password_file"] == "/run/secrets/xian_bds_password"
    assert observed["rpc_url"] == "http://127.0.0.1:26657"
    assert option_value(observed["args"], "--moniker") == moniker
    assert password not in result.stdout


@pytest.mark.parametrize("variable", ["XIAN_BDS_UNKNOWN", "XIAN_BDS_TYPO"])
@pytest.mark.parametrize("source", ["environment", "make"])
def test_unknown_bds_settings_fail_configuration_by_name(configuration_probe, variable, source):
    if source == "make":
        result, observed = configuration_probe(make_arguments=[f"{variable}=old-value"])
    else:
        result, observed = configuration_probe(overrides={variable: "old-value"})

    assert result.returncode != 0
    assert variable in result.stderr
    assert "unknown stack BDS variable" in result.stderr
    assert "old-value" not in result.stderr
    assert observed is None


def test_bds_node_settings_go_through_configure_flags(configuration_probe):
    result, observed = configuration_probe(
        overrides={"CONFIGURE_ARGS": "--moniker m --bds-pool-max-size 20 --bds-rpc-url http://x:1"}
    )

    assert result.returncode == 0, result.stderr
    assert option_value(observed["args"], "--bds-pool-max-size") == "20"
    assert observed["rpc_url"] == "http://x:1"


@pytest.mark.parametrize(
    "overrides, enabled, options",
    [
        (
            {"XIAN_BDS_ENABLED": "1"},
            ["--bds-enabled", "--metrics-enabled"],
            {"--block-policy-mode": "periodic", "--block-policy-interval": "5s"},
        ),
        (
            {"XIAN_BDS_ENABLED": "0", "XIAN_APP_METRICS_ENABLED": "0"},
            ["--no-bds-enabled", "--no-metrics-enabled"],
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
                "XIAN_MAX_TX_CHI": "150000",
                "XIAN_MAX_BLOCK_CHI": "1000000",
                "CONFIGURE_ARGS": "--moniker m --genesis-bundle local --chain-id local-1",
            },
            [],
            {
                "--tx-fee-mode": "free_metered",
                "--max-tx-chi": "150000",
                "--max-block-chi": "1000000",
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
    "XIAN_MAX_TX_CHI": "150000",
    "XIAN_MAX_BLOCK_CHI": "1000000",
}
FEE_POLICY_OPTIONS = {"--tx-fee-mode", "--max-tx-chi", "--max-block-chi"}


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
