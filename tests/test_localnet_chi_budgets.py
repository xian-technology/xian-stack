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
