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
    from xian.utils.encoding import encode_abci_json
    from xian_py.wallet import Wallet
    from xian_runtime_types.time import Datetime

    wallets = {
        role: Wallet(private_key=f"{index:064x}")
        for index, role in enumerate(["alice", "bob", "carol"], start=1)
    }
    accounts = {role: wallet.public_key for role, wallet in wallets.items()}
    names = {role: "con_suite_" + role for role in ROLES}
    scenarios = cases(names, accounts, wallets["alice"].sign_msg("contract-suite authorization"))
    client = ContractingClient(
        storage_home=tmp_path, vm_profile="xian_vm_v2", signer=accounts["alice"]
    )
    driver = client.raw_driver
    driver.commit()

    def execute(contract, function, kwargs, actor="alice", chi=500_000):
        return xian_vm_core.execute_contract(
            driver=driver,
            vm_profile="xian_vm_v2",
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
        # A native success must also satisfy the node's consensus-effects boundary.
        encode_abci_json((result.writes, result.events))
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
    assert ALLOWED_BUILTINS - {"True", "False", "None", "import", "float", "round"} - calls == set()


def test_followup_set_state_preserves_typed_members():
    from contract_suite_cases import followup_cases
    from xian_runtime_types.collections import ContractingFrozenSet, ContractingSet
    from xian_runtime_types.decimal import ContractingDecimal as Decimal
    from xian_runtime_types.encoding import decode_set_value

    stored = ContractingSet([("tuple", 7), ContractingFrozenSet([2, 3]), Decimal("1.2300")])
    scenario = next(
        case for case in followup_cases({"followups": "con_test"}) if case.function == "store_sets"
    )
    for name, value in [("persisted", stored), ("frozen", ContractingFrozenSet(stored))]:
        expected = scenario.state[f"con_test.{name}"]
        assert select_state(value, f"con_test.{name}") == expected
        tag, payload = next(iter(expected.items()))
        decoded = decode_set_value(tag, payload)
        assert type(decoded) is type(value)
        assert ("tuple", 7) in decoded
        assert ContractingFrozenSet([3, 2]) in decoded
        raw = next(item for item in decoded if isinstance(item, Decimal))
        assert str(raw) == "1.23"


def test_followup_budget_failure_retries_the_same_work_successfully():
    from contract_suite_cases import followup_cases

    scenarios = followup_cases({"followups": "con_test"})
    failure, recovery = scenarios[-2:]
    assert failure.function == recovery.function == "hash_callbacks"
    assert failure.error == "out of chi" and recovery.error is None
    assert failure.chi < recovery.chi
    assert recovery.state == {"con_test.result": recovery.result}
    assert recovery.events[0]["data"] == {"mode": "hash budget"}
