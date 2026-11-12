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
    "XIAN_MAX_TX_CHI": "150000",
    "XIAN_MAX_BLOCK_CHI": "400000",
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
        {"tx_fee_mode": "free_metered", "max_tx_chi": 150000, "max_block_chi": 400000}
    ]


def test_chi_caps_apply_to_paid_localnets_and_defaults_fill_the_rest():
    nodes = [localnet.generate_node_material(index) for index in range(1)]
    genesis = localnet.build_localnet_genesis(
        nodes,
        chain_id="xian-localnet-fee-2",
        genesis_network="local",
        runtime_feature_zk=False,
        fee_policy=localnet.genesis_fee_policy({"XIAN_MAX_TX_CHI": "100000"}),
    )

    assert fee_policy_entry(genesis) == [
        {"tx_fee_mode": "paid_metered", "max_tx_chi": 100000, "max_block_chi": 500000}
    ]


def test_smallest_positive_caps_are_accepted():
    environment = {"XIAN_MAX_TX_CHI": "1", "XIAN_MAX_BLOCK_CHI": "1"}
    assert localnet.genesis_fee_policy(environment) == {"max_tx_chi": 1, "max_block_chi": 1}


def test_unset_environment_means_the_paid_genesis_default():
    assert localnet.genesis_fee_policy({}) == {}
    assert localnet.genesis_fee_policy(dict.fromkeys(FREE, "")) == {}


@pytest.mark.parametrize(
    "environment",
    [
        {"XIAN_TX_FEE_MODE": "gratis"},
        {"XIAN_TX_FEE_MODE": "free_metered", "XIAN_MAX_TX_CHI": "lots"},
        {"XIAN_TX_FEE_MODE": "free_metered", "XIAN_MAX_TX_CHI": "0"},
        {"XIAN_MAX_BLOCK_CHI": "-1"},
    ],
)
def test_invalid_fee_policy_environment_is_rejected_before_genesis(environment):
    with pytest.raises(ValueError):
        localnet.genesis_fee_policy(environment)
