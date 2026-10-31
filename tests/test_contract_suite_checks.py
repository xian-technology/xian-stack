"""Ensure the live suite rejects incorrect results, rollback and consensus."""

import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from contract_suite_cases import Case
from localnet_contract_suite import check_execution, check_state, matching_receipts
from localnet_e2e_support import E2EError


def submission(success=True, **overrides):
    execution = {"result": "7", "chi_used": 5, "events": [], "state": []}
    execution.update(overrides)
    return SimpleNamespace(
        accepted=True,
        message=None,
        finalized=True,
        receipt=SimpleNamespace(success=success, execution=execution),
    )


def test_success_requires_exact_result_and_events():
    case = Case("test", "con_a", "call", result=7)
    check_execution(case, submission(), ["con_a"])
    for update in [
        {"result": "8"},
        {"events": [{"contract": "con_a", "event": "unexpected", "data": {}, "data_indexed": {}}]},
        {"chi_used": 0},
    ]:
        with pytest.raises(E2EError):
            check_execution(case, submission(**update), ["con_a"])


def test_failure_requires_reason_no_events_and_no_authored_writes():
    case = Case("test", "con_a", "call", error="deliberate")
    good = submission(
        False, result="deliberate failure", state=[{"key": "currency.balances:alice", "value": "9"}]
    )
    check_execution(case, good, ["con_a", "con_b"])
    for update in [
        {"result": "wrong failure"},
        {"events": [{"event": "leaked"}]},
        {"state": [{"key": "con_b.value", "value": 99}]},
    ]:
        bad = copy.deepcopy(good)
        bad.receipt.execution.update(update)
        with pytest.raises(E2EError):
            check_execution(case, bad, ["con_a", "con_b"])


def test_receipts_compare_every_validator(monkeypatch):
    import localnet_contract_suite as checks

    nodes = list(range(5))
    seen = []

    async def receipt(session, node, tx, *, success):
        seen.append(node)
        return {"tx_result": {"code": 0, "data": "different" if node == 4 else "same"}}

    monkeypatch.setattr(checks, "receipt", receipt)
    with pytest.raises(E2EError, match="receipts disagree"):
        asyncio.run(matching_receipts(SimpleNamespace(nodes=nodes), None, object(), success=True))
    assert sorted(seen) == nodes


def test_state_checks_nested_fields_on_every_validator(monkeypatch):
    import localnet_contract_suite as checks

    nodes = [SimpleNamespace(moniker=f"node-{i}") for i in range(5)]

    async def query(session, node, path):
        assert path == "/get/con_a.orders:purchase"
        return {"status": "corrupted" if node is nodes[-1] else "open"}

    monkeypatch.setattr(checks, "query", query)
    with pytest.raises(E2EError, match="node-4.*state"):
        asyncio.run(
            check_state(
                SimpleNamespace(nodes=nodes), None, {"con_a.orders:purchase/status": "open"}, "test"
            )
        )


def test_minimum_positive_charge_is_valid():
    check_execution(Case("test", "con_a", "call", result=7), submission(chi_used=1), ["con_a"])


def test_equal_receipts_return_committed_height(monkeypatch):
    import localnet_contract_suite as checks

    async def receipt(session, node, tx, *, success):
        return {"height": "42", "tx_result": {"code": 0, "data": "same"}}

    monkeypatch.setattr(checks, "receipt", receipt)
    assert (
        asyncio.run(
            matching_receipts(SimpleNamespace(nodes=list(range(5))), None, object(), success=True)
        )
        == 42
    )


def test_equal_nested_state_passes(monkeypatch):
    import localnet_contract_suite as checks

    async def query(session, node, path):
        return {"status": "open"}

    monkeypatch.setattr(checks, "query", query)
    asyncio.run(
        check_state(
            SimpleNamespace(nodes=[SimpleNamespace(moniker="node")]),
            None,
            {"con_a.orders:purchase/status": "open"},
            "test",
        )
    )


def test_phase_is_in_default_sequence():
    from localnet_e2e_phases import PHASE_SPECS

    spec = next(item for item in PHASE_SPECS if item.name == "03-contract-functionality")
    assert spec.method_name == "contract_functionality_phase" and spec.uses_session


@pytest.mark.parametrize(
    ("info", "raw", "expected"),
    [
        ("decimal", "100", "100"),
        ("decimal", "0.123456789123456789", "0.123456789123456789"),
        ("str", "100", "100"),
        ("str", "true", "true"),
        ("str", "", ""),
        ("int", "100", 100),
        ("bool", "True", True),
        ("bool", "False", False),
        ("list", '["100", 2]', ["100", 2]),
        ("dict", '{"status": "open"}', {"status": "open"}),
        ("", "", None),
    ],
)
def test_query_preserves_scalar_types(monkeypatch, info, raw, expected):
    import base64

    import localnet_contract_suite as checks

    async def rpc(session, node, method, **params):
        return {
            "response": {"code": 0, "info": info, "value": base64.b64encode(raw.encode()).decode()}
        }

    monkeypatch.setattr(checks, "rpc", rpc)
    assert asyncio.run(checks.query(None, None, "/get/con_a.value")) == expected
