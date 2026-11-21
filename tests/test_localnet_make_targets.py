"""The localnet Make targets drive the localnet scripts."""

import os
import subprocess
from pathlib import Path

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


def test_localnet_init_generates_a_clean_localnet_for_the_requested_genesis():
    recipe = dry_run("localnet-init", LOCALNET_NODES="5", XIAN_LOCALNET_GENESIS_NETWORK="local")
    assert "./scripts/localnet-init.py --nodes 5" in recipe
    assert '--genesis-network "local"' in recipe
    assert "--clean" in recipe
