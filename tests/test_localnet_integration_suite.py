import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from localnet_e2e_phases import bind_phase_sequence, phase_names
from test_localnet_block_ordering_phase import localnet_e2e as e2e

EXPECTED = [
    "00-bootstrap",
    "01-health",
    "02-abci-consistency",
    "03-x402-exact",
    "03-intentkit-x402",
]


def test_integration_suite_runs_only_prerequisites_and_payment_checks():
    runner = SimpleNamespace(
        bootstrap=AsyncMock(return_value={}),
        health_phase=AsyncMock(return_value={}),
        abci_consistency_phase=AsyncMock(return_value={}),
        x402_exact_phase=AsyncMock(return_value={}),
        intentkit_x402_phase=AsyncMock(return_value={}),
    )
    session = object()
    sequence = bind_phase_sequence(runner, session, suite="intentkit-x402")
    assert [name for name, _ in sequence] == EXPECTED
    for _, operation in sequence:
        asyncio.run(operation())
    for method in vars(runner).values():
        method.assert_awaited_once_with(session)
    assert len(phase_names()) == 35


@pytest.mark.parametrize("suite", ["unknown", ""])
def test_unknown_suite_cannot_succeed_without_checks(suite):
    with pytest.raises(ValueError, match="suite"):
        phase_names(suite)


def test_focused_suite_rejects_disabled_intentkit_before_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e, "OUTPUT_ROOT", tmp_path)
    args = e2e.build_parser().parse_args(["--suite", "intentkit-x402"])
    runner = e2e.E2ERunner(args)
    runner.bootstrap = AsyncMock()
    with pytest.raises(e2e.E2EError, match="requires --intentkit-x402"):
        asyncio.run(runner.run())
    runner.bootstrap.assert_not_awaited()


def test_required_integration_phase_cannot_be_reported_as_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e, "OUTPUT_ROOT", tmp_path)
    args = e2e.build_parser().parse_args(["--suite", "intentkit-x402", "--intentkit-x402"])
    runner = e2e.E2ERunner(args)
    monkeypatch.setattr(e2e, "collect_failure_diagnostics", AsyncMock(return_value={}))
    with pytest.raises(e2e.E2EError, match="required.*skipped"):
        asyncio.run(
            runner.run_phase("03-intentkit-x402", AsyncMock(return_value={"skipped": True}))
        )
    assert runner.phase_results[-1].ok is False


def test_focused_runner_executes_required_checks_and_records_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e, "OUTPUT_ROOT", tmp_path)
    runner = e2e.E2ERunner(
        e2e.build_parser().parse_args(["--suite", "intentkit-x402", "--intentkit-x402"])
    )
    for method in [
        "bootstrap",
        "health_phase",
        "abci_consistency_phase",
        "x402_exact_phase",
        "intentkit_x402_phase",
    ]:
        setattr(runner, method, AsyncMock(return_value={"checked": True}))
    assert asyncio.run(runner.run()) == 0
    assert [phase.name for phase in runner.phase_results] == EXPECTED
    assert asyncio.run(runner.finalize_summary())["suite"] == "intentkit-x402"


def test_full_suite_retains_optional_skip_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e, "OUTPUT_ROOT", tmp_path)
    runner = e2e.E2ERunner(e2e.build_parser().parse_args([]))
    details = asyncio.run(
        runner.run_phase("03-intentkit-x402", AsyncMock(return_value={"skipped": True}))
    )
    assert details["skipped"] is True
    assert runner.phase_results[-1].ok is True


def test_focused_resume_loads_only_its_own_phase_history(tmp_path, monkeypatch):
    args = e2e.build_parser().parse_args(
        [
            "--suite",
            "intentkit-x402",
            "--intentkit-x402",
            "--start-phase",
            "03-intentkit-x402",
            "--resume-dir",
            str(tmp_path),
        ]
    )
    runner = e2e.E2ERunner(args)
    wallet = e2e.derive_wallet(42, "focused-resume")
    nodes = [
        SimpleNamespace(bds_node=i == 0, account_private_key=wallet.private_key) for i in range(5)
    ]
    monkeypatch.setattr(e2e, "build_nodes", lambda network: nodes)
    (tmp_path / "network.json").write_text(
        json.dumps(
            {
                "founder_key": wallet.private_key,
                "genesis_network": e2e.DEFAULT_GENESIS_NETWORK,
            }
        )
    )
    for name in EXPECTED[:-1]:
        (tmp_path / e2e.json_file_name(name)).write_text(
            json.dumps(
                {
                    "name": name,
                    "ok": True,
                    "started_at": "2026-11-03T00:00:00Z",
                    "ended_at": "2026-11-03T00:00:01Z",
                    "details": {"contract": "con_x402"},
                }
            )
        )
    runner.load_resume_context()
    assert [phase.name for phase in runner.phase_results] == EXPECTED[:-1]
    assert runner.contracts["x402_settlement"] == "con_x402"
