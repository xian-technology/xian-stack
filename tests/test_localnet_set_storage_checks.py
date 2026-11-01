import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from contract_suite_cases import followup_cases
from localnet_e2e_support import E2EError
from localnet_set_storage_checks import (
    authored_set_fixture,
    check_typed_set_storage,
    typed_set_storage_query,
)


def fixture():
    case = next(
        item for item in followup_cases({"followups": "con_sets"}) if item.function == "store_sets"
    )
    phase = SimpleNamespace(
        name="03-contract-functionality",
        ok=True,
        details={
            "contracts": {"followups": "con_sets"},
            "final_state": case.state,
        },
    )
    return phase


def test_resume_fixture_uses_authored_names_without_mutating_phase_evidence():
    phase = fixture()
    original = copy.deepcopy(phase.details)
    contract, expected = authored_set_fixture([phase])
    assert contract == "con_sets" and phase.details == original
    assert expected["con_sets.persisted"]["__set__"]["items"][0] == [
        "tuple",
        [["str", "bds-catchup"], ["int", "7"]],
    ]
    with pytest.raises(E2EError, match="successful authored"):
        authored_set_fixture([])


@pytest.mark.parametrize("table", ["state", "changes"])
@pytest.mark.parametrize("corruption", ["missing", "flattened", "quantum", "numeric", "old_tx"])
def test_bds_set_assertions_reject_lossy_or_stale_rows(table, corruption):
    _, expected = authored_set_fixture([fixture()])
    tx_hash = "a" * 64
    wanted = {
        key: {"value": value, "numeric": None, "tx_hash": tx_hash}
        for key, value in expected.items()
    }
    rows = {"state": copy.deepcopy(wanted), "changes": copy.deepcopy(wanted)}
    assert check_typed_set_storage(rows, expected, tx_hash, label="recovery") == rows
    key = "con_sets.persisted"
    if corruption == "missing":
        del rows[table][key]
    elif corruption == "flattened":
        rows[table][key]["value"] = [["bds-catchup", 7], [2, 3], 1.23]
    elif corruption == "quantum":
        rows[table][key]["value"]["__set__"]["items"][-1] = ["raw_decimal", 0, "123", -2]
    elif corruption == "numeric":
        rows[table][key]["numeric"] = 3
    else:
        rows[table][key]["tx_hash"] = "b" * 64
    with pytest.raises(E2EError, match=f"BDS {table}"):
        check_typed_set_storage(rows, expected, tx_hash, label="recovery")


def test_queries_check_current_state_and_exact_refresh_transaction():
    _, expected = authored_set_fixture([fixture()])
    sql = typed_set_storage_query(expected, "a" * 64)
    assert "FROM state WHERE" in sql and "FROM state_changes WHERE" in sql
    assert "value_numeric" in sql and "new_value_numeric" in sql
    with pytest.raises(E2EError, match="transaction hash"):
        typed_set_storage_query(expected, "' OR 1=1")
    with pytest.raises(E2EError, match="storage key"):
        typed_set_storage_query({"bad'key": {}}, "a" * 64)
