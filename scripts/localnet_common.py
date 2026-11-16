from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Protocol

import aiohttp
import xian_py.transaction as tr
from xian_py import chi_to_supply
from xian_py.xian_async import XianAsync


class RpcNode(Protocol):
    moniker: str
    rpc_url: str


# validators.rebalance does the epoch's selection only once per epoch. A
# simulation runs on the last committed block, where this epoch may already be
# rebalanced (a cheap no-op), while execution lands in the next block and can
# start a new epoch (rebalance_interval 1 on the localnet). No estimate margin
# covers that, so rebalance supplies a budget for the full selection.
REBALANCE_TX_CHI = 20_000

# A governance vote's cost depends on the votes before it: the vote that reaches
# the threshold executes the proposal. The localnet submits votes concurrently,
# so each is simulated before the others (or the proposal) commit. Governance
# transactions supply a budget for executing the proposal (rev-11 validator and
# policy actions used at most ~4,600 chi; state patches more).
GOVERNANCE_TX_CHI = 50_000


async def simulated_chi_supply(
    session: aiohttp.ClientSession,
    rpc_url: str,
    payload: dict[str, Any],
) -> int:
    """The chi a client supplies for ``payload`` (contract, function, kwargs,
    sender, nonce): the simulated chi plus xian-py's margin.

    Unlike xian-py's automatic chi this also sizes a call whose simulation
    fails: the e2e submits failing transactions on purpose."""
    simulation = await tr.simulate_tx_async(
        rpc_url, payload, vm_profile="xian_vm_v2", session=session
    )
    return chi_to_supply(int(simulation["chi_used"]))


def paid_fee_chi(
    rewards: dict[str, dict[str, Any]], chi_cost: Decimal, burn_ratio: Decimal
) -> tuple[int, Decimal]:
    """A paid receipt's chi used and the native value its rewards paid out.

    The receipt's ``chi_used`` is the chi charged: the chi used plus half of
    the supplied chi left unused (revision 14), which is burned. The reward
    split pays ``1 - burn_ratio`` of the fee on the chi used, so the rewards
    give that chi back."""
    if not 0 <= burn_ratio < 1:
        raise ValueError(f"cannot recover the chi used with burn ratio {burn_ratio}")
    paid_out = sum(
        (Decimal(str(value)) for bucket in rewards.values() for value in (bucket or {}).values()),
        Decimal("0"),
    )
    return int((paid_out * chi_cost / (1 - burn_ratio)).to_integral_value()), paid_out


def unused_chi_charge(chi_supplied: int, chi_used: int) -> int:
    """The chi a paid transaction pays for budget it left unused (revision 14)."""
    return max(chi_supplied - chi_used, 0) // 2


class E2EClient(XianAsync):
    """A xian-py client that supplies the simulated chi plus the SDK margin
    when the caller names no chi, like a wallet does, but still submits a
    call whose simulation fails (xian-py refuses those): the e2e puts failing
    transactions on chain on purpose. Fixed budgets are left to the checks
    that test budgets; any other generous budget would pay half of what it
    leaves unused (revision 14's unused-chi charge)."""

    async def send_tx(self, contract, function, kwargs, chi=None, nonce=None, **options):
        if chi is None:
            estimate = await self.estimate_chi(contract, function, kwargs, nonce=nonce)
            chi = chi_to_supply(estimate["estimated"])
        return await super().send_tx(contract, function, kwargs, chi=chi, nonce=nonce, **options)


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
        heights[node.moniker] = int(payload["result"]["sync_info"]["latest_block_height"])

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
            app_hashes[node.moniker] = payload["result"]["block"]["header"]["app_hash"]
        ok = len(set(app_hashes.values())) == 1
        overall_ok = overall_ok and ok
        checks.append({"height": height, "ok": ok, "app_hashes": app_hashes})

    return {"ok": overall_ok, "heights": heights, "checks": checks}
