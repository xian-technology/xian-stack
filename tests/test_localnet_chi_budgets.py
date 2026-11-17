"""Every fixed chi budget in the localnet scripts must fit the chain's default per-tx cap.

A paid-metered chain refuses a transaction whose chi_supplied exceeds
max_tx_chi (xian-abci fee_policy.DEFAULT_MAX_TX_CHI) at CheckTx, before it runs.
"""

import ast
from pathlib import Path

DEFAULT_MAX_TX_CHI = 200_000
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _int_values(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return [node.value]
    if isinstance(node, ast.Dict):
        return [value for item in node.values for value in _int_values(item)]
    return []


def _fixed_budgets(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "chi":
            yield "chi=", node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and "chi" in target.id.lower():
                    yield target.id, node.value


def test_fixed_localnet_chi_budgets_fit_the_default_tx_cap():
    over_cap = []
    for path in sorted(SCRIPTS.glob("*.py")):
        for name, value in _fixed_budgets(ast.parse(path.read_text())):
            over_cap += [
                f"{path.name}:{value.lineno} {name} {chi}"
                for chi in _int_values(value)
                if chi > DEFAULT_MAX_TX_CHI
            ]
    assert over_cap == []


# Fixed budgets kept on purpose. Every other submission supplies the simulated
# chi plus the SDK margin: a paid transaction pays half of the chi it leaves
# unused, so a generous fixed budget drains the test wallets.
DELIBERATE_BUDGETS = {
    # Dependent approve/bond/unbond calls are signed before any H1 execution;
    # their future state cannot be simulated. The raw preflight checks the
    # per-transaction and combined block caps; ordinary SDK calls still estimate.
    ("localnet_idle_unbond_checks.py", "PREFLIGHT_TX_CHI"),
    # rebalance's work depends on the epoch of the block it lands in
    ("localnet_common.py", "REBALANCE_TX_CHI"),
    # a vote that reaches the threshold executes the proposal
    ("localnet_common.py", "GOVERNANCE_TX_CHI"),
    # the parallel probe races writers and scanners into one block
    ("localnet-workload.py", "PARALLEL_PROBE_TX_CHI"),
    # handed to the external IntentKit x402 smoke as its --settlement-chi
    ("localnet-e2e.py", "X402_SETTLEMENT_TX_CHI"),
}
GENEROUS_BUDGET = 5_000


def test_localnet_scripts_supply_simulated_chi_instead_of_generous_budgets():
    generous = []
    for path in sorted(SCRIPTS.glob("*.py")):
        for name, value in _fixed_budgets(ast.parse(path.read_text())):
            if (path.name, name) in DELIBERATE_BUDGETS:
                continue
            generous += [
                f"{path.name}:{value.lineno} {name} {chi}"
                for chi in _int_values(value)
                if chi >= GENEROUS_BUDGET
            ]
    assert generous == []
