import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).absolute().parents[1] / "scripts/node_store_runtime_guard.py"


def invoke(tmp_path, home, rows, *, failure=False):
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os,sys
if os.environ['GUARD_FAILURE']=='1':raise SystemExit(4)
if sys.argv[1]=='ps':print('abc' if os.environ['GUARD_ROWS']!='[]' else '')
elif sys.argv[1]=='inspect':print(os.environ['GUARD_ROWS'])
""")
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "GUARD_ROWS": json.dumps(rows),
        "GUARD_FAILURE": "1" if failure else "0",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--home", str(home)], env=env, capture_output=True, text=True
    )


def test_standalone_asset_reports_known_empty_fresh_home(tmp_path):
    home = tmp_path / "future/node"
    result = invoke(tmp_path, home, [])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"runtime_stopped": True, "home": str(home)}
    assert not home.exists()


@pytest.mark.parametrize("failure", [False, True])
def test_standalone_asset_never_reports_ready_for_live_or_unknown_daemon(tmp_path, failure):
    result = invoke(
        tmp_path,
        tmp_path / "future/node",
        [{"Mounts": [{"Source": str(tmp_path), "RW": True}]}],
        failure=failure,
    )
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("payload", [{}, [], [{}], [{"Mounts": None}]])
def test_incomplete_inspect_payload_cannot_prove_stopped(tmp_path, payload):
    import importlib.util

    spec = importlib.util.spec_from_file_location("runtime_guard_boundary", SCRIPT)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    answers = ["abc", json.dumps(payload)]
    with pytest.raises(ValueError, match="inspect"):
        guard.require_stopped_home(tmp_path, query=lambda *args: answers.pop(0))
