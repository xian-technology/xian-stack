from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import aiohttp


class RpcNode(Protocol):
    moniker: str
    rpc_url: str


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 10.0,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    async with session.get(url, params=params, timeout=timeout) as response:
        return await response.json()


async def compare_app_hash_window(
    session: aiohttp.ClientSession,
    nodes: Sequence[RpcNode],
    *,
    window: int,
) -> dict[str, Any]:
    heights: dict[str, int] = {}
    for node in nodes:
        payload = await fetch_json(session, f"{node.rpc_url}/status", timeout=5.0)
        heights[node.moniker] = int(
            payload["result"]["sync_info"]["latest_block_height"]
        )

    min_height = min(heights.values())
    start_height = max(1, min_height - max(window, 1) + 1)
    checks: list[dict[str, Any]] = []
    overall_ok = True

    for height in range(start_height, min_height + 1):
        app_hashes: dict[str, str] = {}
        for node in nodes:
            payload = await fetch_json(
                session,
                f"{node.rpc_url}/block",
                timeout=5.0,
                params={"height": str(height)},
            )
            app_hashes[node.moniker] = payload["result"]["block"]["header"][
                "app_hash"
            ]
        ok = len(set(app_hashes.values())) == 1
        overall_ok = overall_ok and ok
        checks.append({"height": height, "ok": ok, "app_hashes": app_hashes})

    return {"ok": overall_ok, "heights": heights, "checks": checks}
