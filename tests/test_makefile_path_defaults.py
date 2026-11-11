"""Empty host path settings fall back to stack defaults, as in stack-env.sh."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def dry_run(target, **environment):
    return subprocess.run(
        ["make", "--no-print-directory", "-n", target],
        cwd=ROOT,
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_wipe_bds_with_empty_data_dir_targets_the_stack_default_not_root():
    assert dry_run("wipe-bds", XIAN_BDS_DATA_DIR="").strip() == (f'rm -rf "{ROOT / ".bds.db"}"/*')


@pytest.mark.parametrize(
    "variable, default",
    [
        ("XIAN_COMETBFT_HOME", ".cometbft"),
        ("XIAN_BDS_DATA_DIR", ".bds.db"),
        ("XIAN_CONTRACTS_DIR", "contracts"),
    ],
)
def test_empty_host_paths_resolve_to_stack_defaults(variable, default):
    printed = subprocess.run(
        ["make", "--no-print-directory", "print-env"],
        cwd=ROOT,
        env={**os.environ, variable: ""},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert f"{variable}={ROOT / default}" in printed
