import asyncio
import copy
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from contract_suite_cases import typed_set_state
from localnet_e2e_support import E2EError
from localnet_set_storage_checks import (
    BDS_SET_LABEL,
    check_typed_set_storage,
    deploy_bds_set_fixture,
    typed_set_storage_query,
)


def test_fixture_deploys_independently_and_expects_the_outage_write(monkeypatch):
    runner, session = object(), object()
    deploy = AsyncMock(return_value="con_sets")
    monkeypatch.setattr("localnet_set_storage_checks.deploy", deploy)
    contract, expected = asyncio.run(deploy_bds_set_fixture(runner, session))
    assert contract == "con_sets"
    assert ["tuple", [["str", BDS_SET_LABEL], ["int", "7"]]] in (
        expected["con_sets.persisted"]["__set__"]["items"]
    )
    args = deploy.await_args.args
    assert args[:2] == (runner, session)
    assert "def store_sets" in args[3]


@pytest.mark.parametrize("table", ["state", "changes"])
@pytest.mark.parametrize("corruption", ["missing", "flattened", "quantum", "numeric", "old_tx"])
def test_bds_set_assertions_reject_lossy_or_stale_rows(table, corruption):
    expected = typed_set_state("con_sets", BDS_SET_LABEL)
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
    expected = typed_set_state("con_sets", BDS_SET_LABEL)
    sql = typed_set_storage_query(expected, "a" * 64)
    assert "FROM state WHERE" in sql and "FROM state_changes WHERE" in sql
    assert "value_numeric" in sql and "new_value_numeric" in sql
    with pytest.raises(E2EError, match="transaction hash"):
        typed_set_storage_query(expected, "' OR 1=1")
    with pytest.raises(E2EError, match="storage key"):
        typed_set_storage_query({"bad'key": {}}, "a" * 64)
