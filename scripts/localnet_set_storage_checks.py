"""Require typed wrapper sets to survive real BDS recovery and RPC reindexing."""

import copy
import re

from localnet_e2e_support import E2EError


def authored_set_fixture(phases):
    phase = next((item for item in phases if item.name == "03-contract-functionality"), None)
    if phase is None or not phase.ok:
        raise E2EError("BDS typed-set checks require the successful authored-contract phase")
    contract = phase.details["contracts"]["followups"]
    expected = {
        f"{contract}.{field}": copy.deepcopy(phase.details["final_state"][f"{contract}.{field}"])
        for field in ("persisted", "frozen")
    }
    for value in expected.values():
        payload = next(iter(value.values()))
        if payload["codec"] != "xian-set-1" or payload["items"][0] != [
            "tuple",
            [["str", "tuple"], ["int", "7"]],
        ]:
            raise E2EError("authored typed-set fixture does not contain the expected tuple")
        payload["items"][0] = ["tuple", [["str", "bds-catchup"], ["int", "7"]]]
    return contract, expected


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
