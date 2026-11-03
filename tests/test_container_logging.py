from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("logging_localnet", ROOT / "scripts/localnet-init.py")
localnet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localnet)


@pytest.mark.parametrize("topology", ["integrated", "fidelity"])
def test_generated_localnet_bounds_every_container(tmp_path, topology):
    with patch.object(localnet, "STACK_DIR", tmp_path):
        localnet.write_compose_file(
            [{"index": 0, "moniker": "node-0"}],
            topology,
            bds_enabled=True,
            bds_node_index=0,
            profiling_enabled=False,
            profiling_recent_blocks=20,
        )
    services = json.loads((tmp_path / "docker-compose-localnet.yml").read_text())["services"]
    for service in services.values():
        assert service["logging"] == {
            "driver": "local",
            "options": {
                "max-size": "${XIAN_DOCKER_LOG_MAX_FILE_MB:-50}m",
                "max-file": "${XIAN_DOCKER_LOG_MAX_FILES:-5}",
                "mode": "blocking",
            },
        }


@pytest.mark.parametrize("setting", ["XIAN_DOCKER_LOG_MAX_FILE_MB", "XIAN_DOCKER_LOG_MAX_FILES"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "no"])
def test_invalid_rotation_limits_fail_preflight(setting, value):
    import os
    import subprocess

    result = subprocess.run(
        ["bash", "-c", "source scripts/stack-env.sh; export_stack_env"],
        cwd=ROOT,
        env={**os.environ, setting: value},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert f"{setting} must be a positive integer" in result.stderr
