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
