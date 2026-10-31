import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
import localnet_compatibility_checks as checks


def test_runtime_compatibility_fixture_and_failure_recovery(tmp_path):
    import xian_vm_core
    from contracting.storage.driver import Driver

    driver = Driver(storage_home=tmp_path)
    name = "con_runtime_fixture"
    driver.set_contract_from_source(name, checks.SOURCE, owner="alice")
    driver.set(f"{name}.progress", 0)
    driver.commit()
    for function, progress, failure in [
        ("probe", 1, None),
        ("cyclic_result", 1, "cyclic"),
        ("changed_dictionary", 1, "size"),
        ("probe", 2, None),
    ]:
        result = xian_vm_core.execute_contract(
            driver=driver,
            contract_name=name,
            function_name=function,
            kwargs={},
            context={"signer": "alice", "caller": "alice"},
            meter=False,
        )
        if failure:
            assert result.status_code == 1
            assert failure in str(result.result)
            assert result.writes == {} and result.events == []
        else:
            assert result.status_code == 0, result.result
            assert result.result is True
            for key, value in result.writes.items():
                driver.set(key, value)
        assert driver.get(f"{name}.progress") == progress
        assert driver.get(f"{name}.result") == [1, 2]
        driver.commit()
