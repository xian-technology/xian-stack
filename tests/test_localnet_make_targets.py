"""The localnet Make targets drive the localnet scripts."""

import os
import re
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


def test_every_container_limit_print_env_reports_is_read_by_a_compose_file():
    reported = subprocess.run(
        ["make", "--no-print-directory", "-s", "print-env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    limits = re.findall(r"^(XIAN_DOCKER_\w+)=", reported, re.MULTILINE)
    compose = "".join(path.read_text() for path in ROOT.glob("docker-compose*.yml"))
    assert limits
    assert [name for name in limits if "${" + name not in compose] == []
