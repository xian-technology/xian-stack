"""Live CometBFT RPC checks for application identity and query consistency."""

from __future__ import annotations

import base64
import json

from localnet_common import fetch_json
from localnet_e2e_support import E2EError


def require(condition: bool, message: str) -> None:
    if not condition:
        raise E2EError(message)


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
