import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from localnet_e2e_phases import bind_phase_sequence, phase_names
from test_localnet_parallel_phase import localnet_e2e as e2e

EXPECTED = ["00-bootstrap", "01-health", "03-x402-exact", "03-intentkit-x402"]


def test_integration_suite_runs_only_prerequisites_and_payment_checks():
    runner = SimpleNamespace(
        bootstrap=AsyncMock(return_value={}),
        health_phase=AsyncMock(return_value={}),
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
    assert len(phase_names()) == 36


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
    for method in ["bootstrap", "health_phase", "x402_exact_phase", "intentkit_x402_phase"]:
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
