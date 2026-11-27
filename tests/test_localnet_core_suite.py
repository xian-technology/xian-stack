import asyncio
from unittest.mock import AsyncMock

import pytest
from test_localnet_block_ordering_phase import localnet_e2e as e2e


def test_default_suite_exercises_chain_lifecycle():
    args = e2e.build_parser().parse_args([])
    assert args.suite == "core"
    assert e2e.phase_names(args.suite) == [
        "00-bootstrap",
        "01-health",
        "02-abci-consistency",
        "03-contract-payment",
        "09-bds-chain-continuity",
        "12-governed-fees",
        "16-block-ordering",
        "19-crash-recovery",
        "20-quorum-recovery",
        "21-observer-catchup",
        "23-committed-nonce",
    ]
    assert "26-replay-determinism" in e2e.phase_names("full")
    assert "07-dex-automation-recovery" in e2e.phase_names("dex")
    assert e2e.phase_names("load") == [
        "00-bootstrap",
        "01-health",
        "02-abci-consistency",
        "02-xian-py-smoke",
        "04-periodic-load",
        "05-burst-load",
        "06-conflict-invalid",
        "08-throughput-mix",
        "18-soak-abuse",
    ]


def test_full_summary_distinguishes_optional_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e, "OUTPUT_ROOT", tmp_path)
    runner = e2e.E2ERunner(e2e.build_parser().parse_args(["--suite", "full"]))
    asyncio.run(
        runner.run_phase(
            "03-intentkit-x402", AsyncMock(return_value={"skipped": True, "reason": "disabled"})
        )
    )
    summary = asyncio.run(runner.finalize_summary())
    assert summary["ok"] is True
    assert summary["phases"][0]["status"] == "skipped"
    assert summary["phases"][0]["skip_reason"] == "disabled"


@pytest.mark.parametrize(
    "field, value, error",
    [
        ("chain_id", "other-chain", "chain"),
        ("node_id", "wrong-node", "node ID"),
        ("peers", {"unexpected"}, "peers"),
        ("validator_powers", {"validator-a": 1}, "validator"),
        ("catching_up", True, "catching up"),
        ("validator_powers", {"validator-a": 2, "validator-b": 1}, "validator"),
    ],
)
def test_health_rejects_a_wrong_live_topology(field, value, error):
    from localnet_health_checks import check_topology

    observations = {
        "chain_id": "local-test",
        "node_id": "node-a",
        "peers": {"node-b"},
        "validator_powers": {"validator-a": 1, "validator-b": 1},
        "catching_up": False,
        field: value,
    }
    with pytest.raises(e2e.E2EError, match=error):
        check_topology(
            "node-a",
            observations,
            chain_id="local-test",
            node_id="node-a",
            peers={"node-b"},
            validator_powers={"validator-a": 1, "validator-b": 1},
        )


def test_topology_accepts_the_expected_peer_and_validator_set():
    from localnet_health_checks import check_topology

    check_topology(
        "node-a",
        {
            "chain_id": "local-test",
            "node_id": "node-a",
            "peers": {"node-b"},
            "validator_powers": {"validator-a": 1, "validator-b": 1},
            "catching_up": False,
        },
        chain_id="local-test",
        node_id="node-a",
        peers={"node-b"},
        validator_powers={"validator-a": 1, "validator-b": 1},
    )
