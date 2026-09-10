from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/localnet-init.py"
SPEC = importlib.util.spec_from_file_location("localnet_storage_init", SCRIPT)
localnet_init = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localnet_init)


@pytest.mark.parametrize("topology", ["integrated", "fidelity"])
def test_bds_volume_is_unique_per_generation_and_preserves_node_mounts(tmp_path, topology):
    def generate():
        with patch.object(localnet_init, "STACK_DIR", tmp_path):
            localnet_init.write_compose_file(
                [{"index": 0, "moniker": "node-0"}], topology,
                bds_enabled=True, bds_node_index=0,
                profiling_enabled=False, profiling_recent_blocks=20,
            )
        return json.loads((tmp_path / "docker-compose-localnet.yml").read_text())

    first, second = generate(), generate()
    alias = "localnet-postgres-data"
    assert first["volumes"][alias]["name"] != second["volumes"][alias]["name"]
    db = first["services"]["localnet-postgres"]
    assert db["volumes"] == [f"{alias}:/var/lib/postgresql/data"]
    assert "SELECT 1" in db["healthcheck"]["test"][-1]
    app = first["services"]["node-0" if topology == "integrated" else "node-0-abci"]
    assert "./.localnet/node-0/.cometbft:/root/.cometbft" in app["volumes"]
    assert app["depends_on"]["localnet-postgres"]["condition"] == "service_healthy"


def test_no_database_volume_when_bds_is_disabled(tmp_path):
    with patch.object(localnet_init, "STACK_DIR", tmp_path):
        localnet_init.write_compose_file(
            [{"index": 0, "moniker": "node-0"}], "integrated",
            bds_enabled=False, bds_node_index=0,
            profiling_enabled=False, profiling_recent_blocks=20,
        )
    compose = json.loads((tmp_path / "docker-compose-localnet.yml").read_text())
    assert "volumes" not in compose
    assert "localnet-postgres" not in compose["services"]
