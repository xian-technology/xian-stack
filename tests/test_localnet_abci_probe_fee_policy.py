"""The isolated ABCI probe uses the chain's genesis fee policy, not node config."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fee_policy_probe", ROOT / "scripts/localnet_abci_probe.py"
)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_probe_reads_the_fee_policy_from_genesis(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/genesis.json").write_text(
        json.dumps(
            {
                "abci_genesis": {
                    "genesis": [
                        {
                            "key": "__fee_policy",
                            "value": {
                                "tx_fee_mode": "free_metered",
                                "free_tx_max_chi": 250000,
                                "free_block_max_chi": 5000000,
                            },
                        }
                    ]
                }
            }
        )
    )
    (tmp_path / "config/xian.toml").write_text("simulation_enabled = true\n")

    policy = probe.chain_fee_policy(tmp_path)

    assert policy.to_state() == {
        "tx_fee_mode": "free_metered",
        "free_tx_max_chi": 250000,
        "free_block_max_chi": 5000000,
    }
