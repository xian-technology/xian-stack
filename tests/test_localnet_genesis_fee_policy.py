"""Localnet genesis carries the fee policy chosen through the stack environment."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fee_localnet", ROOT / "scripts/localnet-init.py")
localnet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(localnet)

FREE = {
    "XIAN_TX_FEE_MODE": "free_metered",
    "XIAN_FREE_TX_MAX_CHI": "250000",
    "XIAN_FREE_BLOCK_MAX_CHI": "5000000",
}


def fee_policy_entry(genesis: dict):
    entries = genesis["abci_genesis"]["genesis"]
    return [entry["value"] for entry in entries if entry["key"] == "__fee_policy"]


def test_environment_fee_policy_reaches_the_localnet_genesis():
    nodes = [localnet.generate_node_material(index) for index in range(4)]
    genesis = localnet.build_localnet_genesis(
        nodes,
        chain_id="xian-localnet-fee-1",
        genesis_network="local",
        runtime_feature_zk=False,
        fee_policy=localnet.genesis_fee_policy(FREE),
    )

    assert fee_policy_entry(genesis) == [
        {"tx_fee_mode": "free_metered", "free_tx_max_chi": 250000, "free_block_max_chi": 5000000}
    ]


def test_unset_environment_means_the_paid_genesis_default():
    assert localnet.genesis_fee_policy({}) == {}
    assert localnet.genesis_fee_policy(dict.fromkeys(FREE, "")) == {}


@pytest.mark.parametrize(
    "environment",
    [
        {"XIAN_TX_FEE_MODE": "gratis"},
        {"XIAN_TX_FEE_MODE": "free_metered", "XIAN_FREE_TX_MAX_CHI": "lots"},
        {"XIAN_TX_FEE_MODE": "free_metered", "XIAN_FREE_TX_MAX_CHI": "0"},
        {"XIAN_FREE_TX_MAX_CHI": "250000"},
    ],
)
def test_invalid_fee_policy_environment_is_rejected_before_genesis(environment):
    with pytest.raises(ValueError):
        localnet.genesis_fee_policy(environment)
