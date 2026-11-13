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
                                "max_tx_chi": 150000,
                                "max_block_chi": 400000,
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
        "max_tx_chi": 150000,
        "max_block_chi": 400000,
    }


PAID_POLICY = [
    {
        "key": "__fee_policy",
        "value": {"tx_fee_mode": "paid_metered", "max_tx_chi": 200000, "max_block_chi": 500000},
    }
]


def write_node_home(home: Path, genesis_entries: list[dict]) -> None:
    (home / ".cometbft/config").mkdir(parents=True)
    (home / ".cometbft/config/genesis.json").write_text(
        json.dumps({"abci_genesis": {"genesis": genesis_entries}})
    )
    (home / ".cometbft/config/xian.toml").write_text("max_tx_bytes = 4096\n")


def test_probe_passes_against_the_installed_runtime(tmp_path, monkeypatch):
    import asyncio

    write_node_home(tmp_path, PAID_POLICY)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    report = asyncio.run(probe.probe("xian-probe-1", probe.__version__))

    assert report["application_version"] == probe.__version__
    assert [item["tx_count"] for item in report["proposal_budgets"]] == [0, 1, 2]
    assert report["proposal_budgets"][0] == {"budget": 0, "returned_bytes": 0, "tx_count": 0}
    assert report["skipped_nonce_preserved"] is True
    assert [item["accepted"] for item in report["submission_names"]] == [False, False, True, True]
    assert [item["name"] for item in report["invalid_txs"]] == ["con_Uppercase", "con_" + "a" * 61]
    # The node image ships the native fast path; this test venv may not.
    assert report["native_admission"] is (
        probe._native_decode_and_validate_transaction_static is not None
    )


def test_probe_refuses_an_unexpected_application_version(tmp_path, monkeypatch):
    import asyncio

    import pytest

    write_node_home(tmp_path, PAID_POLICY)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="unexpected installed application version"):
        asyncio.run(probe.probe("xian-probe-1", "0.0.0-not-installed"))
