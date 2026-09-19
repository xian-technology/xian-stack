from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import localnet_network as network


def test_partition_fixture_pins_unique_addresses_and_preserves_aliases(tmp_path):
    path = tmp_path / "compose.json"
    path.write_text(
        json.dumps(
            {
                "networks": {"localnet": {"driver": "bridge"}},
                "services": {
                    "node-0": {"networks": ["localnet"]},
                    "node-3": {"networks": {"localnet": {"aliases": ["validator"]}}},
                    "postgres": {"networks": ["localnet"]},
                },
            }
        )
    )
    calls = []

    async def command(*args):
        calls.append(args)
        if args[2] == "inspect":
            return json.dumps(
                [{"IPAM": {"Config": [{"Subnet": "172.29.0.0/24", "Gateway": "172.29.0.1"}]}}]
            )
        return ""

    with patch.object(network, "command", command):
        result = asyncio.run(network.pin_localnet_addresses(path))
        # A second invocation keeps addresses and does not allocate another subnet.
        assert asyncio.run(network.pin_localnet_addresses(path)) == result
    compose = json.loads(path.read_text())
    assert len(calls) == 3
    assert calls[-1][2] == "rm"
    addresses = list(result["addresses"].values())
    assert len(set(addresses)) == 3
    assert "172.29.0.1" not in addresses
    assert compose["services"]["node-3"]["networks"]["localnet"] == {
        "aliases": ["validator"],
        "ipv4_address": result["addresses"]["node-3"],
    }
    assert compose["networks"]["localnet"]["ipam"]["config"][0]["subnet"] == "172.29.0.0/24"


def test_failed_subnet_inspection_cleans_up_without_publishing_partial_compose(tmp_path):
    path = tmp_path / "compose.json"
    original = '{"networks":{"localnet":{}},"services":{}}'
    path.write_text(original)
    calls = []

    async def command(*args):
        calls.append(args)
        if args[2] == "inspect":
            raise RuntimeError("Docker unavailable")
        return ""

    with patch.object(network, "command", command), pytest.raises(RuntimeError):
        asyncio.run(network.pin_localnet_addresses(path))
    assert calls[-1][2] == "rm"
    assert path.read_text() == original
