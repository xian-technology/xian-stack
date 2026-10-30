import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from localnet_e2e_support import E2EError
from localnet_runtime_safety_checks import failed_execution


def result(*, success=False, finalized=True, chi=12, message="denied"):
    return SimpleNamespace(
        finalized=finalized,
        receipt=SimpleNamespace(success=success, execution={"chi_used": chi, "result": message}),
    )


@pytest.mark.parametrize(
    "submission",
    [result(success=True), result(finalized=False), result(chi=0)],
)
def test_failure_oracle_requires_committed_failure_and_charged_work(submission):
    with pytest.raises(E2EError):
        failed_execution(submission, "guard")


def test_failure_oracle_rejects_wrong_failure_reason_and_undercharged_exhaustion():
    with pytest.raises(E2EError, match="reason"):
        failed_execution(result(), "guard", fragment="out of chi")
    with pytest.raises(E2EError, match="budget"):
        failed_execution(result(message="out of chi"), "budget", budget=5000)
    actual = failed_execution(
        result(chi=5000, message="out of chi"), "budget", fragment="out of chi", budget=5000
    )
    assert actual["chi_used"] == 5000


@pytest.mark.parametrize(
    "source_name",
    [
        "CHILD_SOURCE",
        "FACTORY_SOURCE",
        "METADATA_SOURCE",
        "METER_SOURCE",
        "STORAGE_SOURCE",
        "REGISTRAR_SOURCE",
    ],
)
def test_live_fixture_sources_compile_with_canonical_compiler(source_name):
    import localnet_runtime_safety_checks as checks
    from contracting.compilation.artifacts import compile_contract_source

    compiled = compile_contract_source(
        module_name="con_runtime_fixture", source=getattr(checks, source_name)
    )
    assert compiled["vm_ir_json"]


def test_uniform_state_rejects_divergence_on_any_validator(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    import localnet_runtime_safety_checks as checks

    runner = SimpleNamespace(
        nodes=[SimpleNamespace(moniker="node-0"), SimpleNamespace(moniker="node-1")]
    )
    monkeypatch.setattr(checks, "query", AsyncMock(side_effect=["original", "changed"]))
    with pytest.raises(E2EError, match="node-1"):
        asyncio.run(checks.uniform_state(runner, object(), {"con_child.__owner__": "original"}))


def test_minimal_charged_failure_and_success_are_distinct():
    from localnet_runtime_safety_checks import successful

    assert failed_execution(result(chi=1), "one unit")["chi_used"] == 1
    with pytest.raises(E2EError):
        successful(result(), "failed transaction")
    with pytest.raises(E2EError):
        successful(result(success=True, finalized=False), "uncommitted transaction")
    assert successful(result(success=True), "success")["chi_used"] == 12


@pytest.mark.parametrize(
    "message, accepted_reason",
    [
        ("TransactionException: Transaction chi_supplied must be greater than zero", True),
        ("TransactionException: Payload key 'chi_supplied' is missing", True),
        ("TransactionException: Wrong nonce", False),
    ],
)
def test_zero_chi_probe_accepts_backend_diagnostics_and_reuses_nonce(
    monkeypatch, message, accepted_reason
):
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import localnet_runtime_safety_checks as checks

    valid = result(success=True)
    valid.tx_hash = "recovery"
    client = SimpleNamespace(send_tx=AsyncMock(return_value=valid))

    @asynccontextmanager
    async def connected(*args):
        yield client

    runner = SimpleNamespace(
        seed="zero-budget-test", run_id="run", network={"chain_id": "local-test"},
        founder_wallet=SimpleNamespace(public_key="a" * 64),
        fund_wallets=AsyncMock(), client=connected,
        nodes=[SimpleNamespace(moniker=f"node-{i}") for i in range(5)],
    )
    broadcast = AsyncMock(return_value={"code": 1, "log": message})
    monkeypatch.setattr(checks, "broadcast", broadcast)
    monkeypatch.setattr(checks, "agreement", AsyncMock())
    monkeypatch.setattr(checks, "uniform_state", AsyncMock())
    if not accepted_reason:
        with pytest.raises(E2EError, match="wrong reason"):
            asyncio.run(checks.zero_chi_check(runner, object()))
        client.send_tx.assert_not_awaited()
        return
    output = asyncio.run(checks.zero_chi_check(runner, object()))
    assert len(output["rejected"]) == broadcast.await_count == 5
    for call in broadcast.await_args_list:
        assert call.kwargs["accepted"] is False
    client.send_tx.assert_awaited_once()
    assert client.send_tx.await_args.kwargs["nonce"] == 0
    assert client.send_tx.await_args.kwargs["chi"] > 0
    assert output["recovery_tx"] == "recovery"
