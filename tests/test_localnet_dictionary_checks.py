import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))

import localnet_dictionary_checks as checks
from localnet_e2e_support import E2EError


def test_live_dictionary_contract_executes_and_rolls_back(tmp_path):
    import xian_vm_core
    from contracting.storage.driver import Driver

    driver = Driver(storage_home=tmp_path)
    name = "con_dictionary_fixture"
    driver.set_contract_from_source(name, checks.SOURCE, owner="alice")
    driver.set(f"{name}.progress", 0)
    driver.commit()
    for function, progress in [("probe", 1), ("invalid", 1), ("probe", 2)]:
        output = xian_vm_core.execute_contract(
            driver=driver,
            contract_name=name,
            function_name=function,
            kwargs={},
            context={"signer": "alice", "caller": "alice"},
            meter=False,
        )
        if function == "invalid":
            assert output.status_code == 1
            assert "unhashable" in str(output.result)
            assert output.writes == {}
            assert output.events == []
        else:
            assert output.status_code == 0, output.result
            assert output.result == [1, 3, 1, 2, 2, [[2]]]
            assert output.writes[f"{name}.progress"] == progress
            for key, value in output.writes.items():
                driver.set(key, value)
        assert driver.get(f"{name}.progress") == progress
        driver.commit()


@pytest.mark.parametrize("different", [False, True])
def test_receipt_agreement_checks_every_validator(monkeypatch, different):
    results = [{"tx_result": {"code": 0, "data": "result"}} for _ in range(5)]
    if different:
        results[-1] = {"tx_result": {"code": 0, "data": "wrong"}}
    fetch = AsyncMock(side_effect=results)
    monkeypatch.setattr(checks, "receipt", fetch)
    task = checks.matching_receipts(
        SimpleNamespace(nodes=list(range(5))),
        object(),
        object(),
        success=True,
    )
    if different:
        with pytest.raises(E2EError, match="receipts disagree"):
            asyncio.run(task)
    else:
        asyncio.run(task)
    assert fetch.await_count == 5
