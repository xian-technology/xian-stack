"""Run the authored application flows through actual native source deployment."""

import ast
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from contract_suite_cases import ROLES, cases, constructors, initial_state, select_state, sources
from localnet_e2e_support import normalize_value


def test_native_contract_suite(tmp_path):
    import xian_vm_core
    from contracting.local import ContractingClient
    from contracting.storage.driver import Driver
    from xian_py.wallet import Wallet
    from xian_runtime_types.time import Datetime

    wallets = {
        role: Wallet(private_key=f"{index:064x}")
        for index, role in enumerate(["alice", "bob", "carol"], start=1)
    }
    accounts = {role: wallet.public_key for role, wallet in wallets.items()}
    names = {role: "con_suite_" + role for role in ROLES}
    scenarios = cases(names, accounts, wallets["alice"].sign_msg("contract-suite authorization"))
    client = ContractingClient(driver=Driver(storage_home=tmp_path), signer=accounts["alice"])
    driver = client.raw_driver
    driver.commit()

    def execute(contract, function, kwargs, actor="alice", chi=500_000):
        return xian_vm_core.execute_contract(
            driver=driver,
            contract_name=contract,
            function_name=function,
            kwargs=kwargs,
            context={
                "signer": accounts[actor],
                "caller": accounts[actor],
                "this": contract,
                "now": Datetime(2026, 10, 31, 12),
                "block_num": 7,
                "block_hash": "ab" * 32,
                "chain_id": "xian-suite-local",
            },
            meter=True,
            chi_budget_raw=chi * 1000,
        )

    def commit(result):
        for key, value in result.writes.items():
            driver.set(key, value)
        driver.commit()

    for role, source in sources(names).items():
        result = execute(
            "submission",
            "submit_contract",
            {
                "name": names[role],
                "code": source,
                "constructor_args": constructors(accounts).get(role, {}),
            },
        )
        assert result.status_code == 0, (role, result.result)
        commit(result)
    expected = initial_state(names, accounts, scenarios)

    def check_state(label):
        for selector, value in expected.items():
            actual = select_state(driver.get(selector.split("/")[0]), selector)
            assert normalize_value(actual) == normalize_value(value), (
                label,
                selector,
                actual,
                value,
            )

    check_state("constructors")
    for case in scenarios:
        result = execute(case.contract, case.function, case.kwargs, case.actor, case.chi)
        assert result.chi_used > 0, case.label
        if case.error is not None:
            assert result.status_code == 1, (case.label, result.result)
            assert case.error in str(result.result).lower(), (case.label, result.result)
            assert result.writes == {} and result.events == [], case.label
            driver.rollback()
        else:
            assert result.status_code == 0, (case.label, result.result)
            assert normalize_value(result.result) == normalize_value(case.result), (
                case.label,
                result.result,
            )
            actual_events = [
                dict(
                    contract=item["contract"],
                    event=item["event"],
                    data={**item["data"], **item["data_indexed"]},
                )
                for item in result.events
            ]
            assert normalize_value(actual_events) == list(case.events), (case.label, actual_events)
            commit(result)
            expected.update(copy.deepcopy(case.state))
        check_state(case.label)


def test_contract_suite_has_nominal_builtin_coverage():
    from contracting.compilation.whitelists import ALLOWED_BUILTINS

    authored = sources({role: "con_suite_" + role for role in ROLES})
    calls = {
        node.func.id
        for source in authored.values()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert ALLOWED_BUILTINS - {"True", "False", "None", "import"} - calls == set()
