"""E2EClient supplies chi like a wallet but still submits failing calls."""

import asyncio
from unittest.mock import AsyncMock, patch

from localnet_common import E2EClient, simulated_chi_supply
from xian_py import CHI_ESTIMATE_MARGIN
from xian_py.wallet import Wallet
from xian_py.xian_async import XianAsync


def _client() -> E2EClient:
    return E2EClient("http://node", chain_id="xian-local-1", wallet=Wallet())


def _send(client: E2EClient, **options):
    async def run():
        try:
            return await client.send_tx("con_probe", "fail", {}, **options)
        finally:
            await client.close()

    return asyncio.run(run())


def test_without_chi_it_supplies_the_estimate_plus_the_margin_even_for_a_failing_call():
    client = _client()
    failing = {"estimated": 321, "simulation": {"status": 1, "chi_used": 321}}
    with (
        patch.object(XianAsync, "estimate_chi", AsyncMock(return_value=failing)) as estimate,
        patch.object(XianAsync, "send_tx", AsyncMock(return_value="sent")) as send,
    ):
        assert _send(client, nonce=7) == "sent"

    estimate.assert_awaited_once_with("con_probe", "fail", {}, nonce=7)
    assert send.await_args.kwargs["chi"] == 321 + CHI_ESTIMATE_MARGIN
    assert send.await_args.kwargs["nonce"] == 7


def test_an_explicit_budget_is_supplied_unchanged():
    client = _client()
    with (
        patch.object(XianAsync, "estimate_chi", AsyncMock()) as estimate,
        patch.object(XianAsync, "send_tx", AsyncMock(return_value="sent")) as send,
    ):
        _send(client, chi=80)

    estimate.assert_not_awaited()
    assert send.await_args.kwargs["chi"] == 80


def test_simulated_chi_supply_sizes_a_failing_payload():
    payload = {"contract": "con_probe", "function": "fail", "kwargs": {}, "sender": "a", "nonce": 3}
    simulate = AsyncMock(return_value={"status": 1, "chi_used": 40})
    with patch("localnet_common.tr.simulate_tx_async", simulate):
        supply = asyncio.run(simulated_chi_supply(None, "http://node", payload))

    assert supply == 40 + CHI_ESTIMATE_MARGIN
    assert simulate.await_args.args == ("http://node", payload)


def test_paid_fee_chi_recovers_the_used_chi_from_the_reward_split():
    from decimal import Decimal

    from localnet_common import paid_fee_chi, unused_chi_charge

    # 180 chi used at 20 chi per token, split 0.6/0.1 burn/0.1/0.2, supplied 182.
    rewards = {
        "validator_reward": {"op": "4.4"},
        "delegator_pool_reward": {"validators": "1.0"},
        "foundation_reward": {"owner": "0.9"},
        "developer_reward": {"dev": "1.8"},
    }
    used, paid_out = paid_fee_chi(rewards, Decimal(20), Decimal("0.1"))
    assert (used, paid_out) == (180, Decimal("8.1"))
    assert unused_chi_charge(182, used) == 1
    assert unused_chi_charge(180, 180) == 0
    assert unused_chi_charge(181, 182) == 0  # ran out of chi: nothing unused


def test_paid_fee_chi_refuses_a_split_that_burns_everything():
    from decimal import Decimal

    import pytest
    from localnet_common import paid_fee_chi

    with pytest.raises(ValueError):
        paid_fee_chi({}, Decimal(20), Decimal(1))
