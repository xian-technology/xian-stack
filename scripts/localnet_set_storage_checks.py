"""Require typed wrapper sets to survive real BDS recovery and RPC reindexing."""

import re

from contract_suite_cases import typed_set_state
from localnet_chain_checks import deploy
from localnet_e2e_support import E2EError

BDS_SET_LABEL = "bds-catchup"
BDS_SET_SOURCE = """persisted = Variable()
frozen = Variable()

@export
def store_sets(label: str):
    value = set([(label, 7), frozenset([2, 3]), 1.23])
    persisted.set(value)
    frozen.set(frozenset(value))
"""


async def deploy_bds_set_fixture(runner, session):
    contract = await deploy(runner, session, "bds-typed-sets", BDS_SET_SOURCE)
    return contract, typed_set_state(contract, BDS_SET_LABEL)


def typed_set_storage_query(expected, tx_hash):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", tx_hash):
        raise E2EError("typed-set query requires a transaction hash")
    if not expected or any(
        not re.fullmatch(r"[a-zA-Z0-9_]+\.(persisted|frozen)", key) for key in expected
    ):
        raise E2EError("typed-set query contains an invalid storage key")
    keys = ", ".join(f"'{key}'" for key in expected)
    return f"""SELECT jsonb_build_object(
        'state', COALESCE((SELECT jsonb_object_agg(key, jsonb_build_object(
            'value', value, 'numeric', value_numeric, 'tx_hash', last_tx_hash))
            FROM state WHERE key IN ({keys})), '{{}}'::jsonb),
        'changes', COALESCE((SELECT jsonb_object_agg(key, jsonb_build_object(
            'value', new_value, 'numeric', new_value_numeric, 'tx_hash', tx_hash))
            FROM state_changes WHERE key IN ({keys}) AND tx_hash = '{tx_hash}'), '{{}}'::jsonb)
    );"""


def check_typed_set_storage(rows, expected, tx_hash, *, label):
    wanted = {
        key: {"value": value, "numeric": None, "tx_hash": tx_hash}
        for key, value in expected.items()
    }
    for table in ("state", "changes"):
        if rows.get(table) != wanted:
            raise E2EError(
                f"{label}: BDS {table} did not preserve typed sets and null numeric values"
            )
    return rows
