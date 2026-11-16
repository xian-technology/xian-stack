"""Exercise the actual status entrypoint without a Docker daemon or user node."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).absolute().parents[1]


def status(tmp_path, *, rows="[]", exit_code=0, topology="fidelity"):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/node-status.sh", scripts / "node-status.sh")
    (scripts / "stack-env.sh").write_text("export_stack_env() { :; }\n")
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os,sys
if "ps" in sys.argv:
    print(os.environ["FAKE_COMPOSE_ROWS"])
    raise SystemExit(int(os.environ["FAKE_COMPOSE_EXIT"]))
raise SystemExit(1)
""")
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "FAKE_COMPOSE_ROWS": rows,
        "FAKE_COMPOSE_EXIT": str(exit_code),
        "XIAN_STACK_TOPOLOGY": topology,
    }
    result = subprocess.run(
        ["bash", str(scripts / "node-status.sh")], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_failed_compose_query_cannot_prove_stopped(tmp_path):
    result = status(tmp_path, exit_code=1)
    assert result["runtime_status_known"] is False
    assert result["backend_running"] is False


def test_successful_empty_runtime_query_is_known(tmp_path):
    result = status(tmp_path)
    assert result["runtime_status_known"] is True
    assert result["compose_services"] == []


@pytest.mark.parametrize("state", ["running", "paused", "restarting", "exited"])
def test_partial_fidelity_runtime_preserves_every_observed_state(tmp_path, state):
    rows = json.dumps([{"Service": "abci-app", "State": state}])
    result = status(tmp_path, rows=rows)
    assert result["runtime_status_known"] is True
    assert result["runtime_containers"] == ["abci-app", "cometbft"]
    assert result["compose_services"][0]["state"] == state
    assert result["runtime_services_running"] is False
