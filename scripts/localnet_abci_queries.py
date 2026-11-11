"""Live CometBFT RPC checks for application identity and query consistency."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import xian_compiler_core
from contracting.local import ContractingClient
from localnet_common import fetch_json
from localnet_e2e_support import E2EError
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import normalize_wire_numbers

QUOTE_INCREMENT = ContractingDecimal("0.000000000000000000000000000001")
EXPECTED_QUOTE = {"__fixed__": "9007199254740993.125000000000000000000000000001"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise E2EError(message)


def local_numeric_ir(name: str, code: str) -> str:
    """Compile and execute through both ordinary authoring entry points."""
    artifact = xian_compiler_core.compile_contract_artifact(name, code)
    require(artifact["vm_profile"] == "xian_vm_v2", "default compiler did not select V2")
    with tempfile.TemporaryDirectory() as home, ContractingClient(storage_home=Path(home)) as local:
        local.submit(code, name=name)
        require(
            local.get_contract_ir(name) == artifact["vm_ir_json"], "local VM and compiler IR differ"
        )
        quote = local.get_contract_proxy(name).quote(amount=QUOTE_INCREMENT)
        require(normalize_wire_numbers(quote) == EXPECTED_QUOTE, "local VM lost exact quote")
    return artifact["vm_ir_json"]


async def check_live_abci(session, node, *, expected_version, state_path, invalid_txs):
    async def rpc(method, params=None):
        payload = await fetch_json(session, f"{node.rpc_url}/{method}", params=params)
        require("result" in payload, f"{node.moniker}: RPC {method} failed: {payload}")
        return payload["result"]

    async def query(height=0, prove=False):
        return (
            await rpc(
                "abci_query",
                {
                    "path": json.dumps(state_path),
                    "height": str(height),
                    "prove": "true" if prove else "false",
                },
            )
        )["response"]

    before = (await rpc("abci_info"))["response"]
    require(before["version"] == expected_version, f"{node.moniker}: wrong application version")
    require(int(before["last_block_height"]) > 1, "live checks require the confirmed deployment")
    latest = await query()
    after = (await rpc("abci_info"))["response"]
    height = int(latest["height"])
    require(int(latest.get("code", 0)) == 0, f"{node.moniker}: latest-state query failed")
    require(
        int(before["last_block_height"]) <= height <= int(after["last_block_height"]),
        f"{node.moniker}: query height is outside committed height range",
    )
    require(
        base64.b64decode(latest["value"]) == b"7", "deployed constructor state was not returned"
    )

    for requested, prove, message in [
        (1, False, "Unsupported query height"),
        (int(after["last_block_height"]) + 1_000_000, False, "Unsupported query height"),
        (0, True, "Merkle proof queries are not supported"),
    ]:
        result = await query(requested, prove)
        require(
            int(result.get("code", 0)) != 0 and message in result.get("log", ""),
            f"{node.moniker}: unsupported query was not rejected: {result}",
        )
        require(
            int(result["height"]) >= height and not result.get("value"),
            "rejected query lost committed height or returned state",
        )

    for _ in range(10):
        current = int((await query())["height"])
        exact = await query(current)
        if int(exact.get("code", 0)) == 0:
            require(int(exact["height"]) == current, "exact query returned a different height")
            break
        require(
            int(exact["height"]) > current and "Unsupported query height" in exact.get("log", ""),
            "exact-height query failed without a concurrent commit",
        )
    else:
        raise E2EError("could not exercise exact-current-height query")

    rejected_names = []
    for item in invalid_txs:
        response = await rpc("broadcast_tx_sync", {"tx": "0x" + item["tx_hex"]})
        require(
            int(response.get("code", 0)) != 0
            and "contract name is invalid" in response.get("log", ""),
            f"{node.moniker}: CheckTx did not reject invalid name {item['name']}",
        )
        rejected_names.append(item["name"])
    return {
        "application_version": before["version"],
        "committed_height": height,
        "query_height_and_errors": True,
        "rejected_names": rejected_names,
    }
