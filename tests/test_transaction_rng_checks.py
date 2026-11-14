"""Pin the node RNG oracle and exercise the authored probe through native execution."""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest
from contract_suite_cases import SOURCE_DIR, Case
from localnet_contract_suite import check_execution
from localnet_e2e_support import E2EError
from localnet_rng_checks import transaction_random_value
from test_contract_suite_checks import submission

BLOCK = {
    "block_id": {"hash": "AB" * 32},
    "block": {
        "header": {
            "height": "17",
            "chain_id": "local-rng-audit",
            "time": "2026-11-03T12:34:56.123456789Z",
        }
    },
}


def test_oracle_rejects_dropped_transaction_seed():
    expected = transaction_random_value(BLOCK, "signature-one")
    seed = f"chain_id=local-rng-audit|block_num=17|block_hash={'ab' * 32}|input_hash=0|0"
    missing = int.from_bytes(hashlib.sha3_256(seed.encode()).digest()[:8], "big")
    with pytest.raises(E2EError, match="wrong result"):
        check_execution(
            Case("transaction-rng", "con_rng", "transaction_random", result=expected),
            submission(result={"__big_int__": str(missing)}),
            ["con_rng"],
        )
    assert expected != transaction_random_value(BLOCK, "signature-two")
    rounded = {
        **BLOCK,
        "block": {"header": {**BLOCK["block"]["header"], "time": "2026-11-03T12:34:56.123456Z"}},
    }
    assert expected != transaction_random_value(rounded, "signature-one")


def test_authored_rng_uses_each_explicit_transaction_context(tmp_path):
    from contracting.storage.driver import Driver
    from vm_call_support import process_transaction
    from xian.fee_policy import TxFeePolicy

    with Driver(vm_profile="xian_vm_v2", storage_home=tmp_path) as driver:
        driver.set_contract_from_source("con_rng", (SOURCE_DIR / "commitments.py").read_text())
        driver.commit()
        for nonce, signature in enumerate(["signature-one", "signature-two"]):
            tx = {
                "payload": {
                    "sender": "alice",
                    "contract": "con_rng",
                    "function": "transaction_random",
                    "kwargs": {},
                    "nonce": nonce,
                    "chi_supplied": 100_000,
                },
                "metadata": {"signature": signature},
                "b_meta": {
                    "nanos": 1793709296123456789,
                    "height": 17,
                    "hash": "ab" * 32,
                    "chain_id": "local-rng-audit",
                },
            }
            # The node's own path: the Rust executor reads the block time and
            # the transaction's input hash from the block and the signature.
            receipt = process_transaction(driver, tx, TxFeePolicy.free_metered())
            expected = transaction_random_value(BLOCK, signature)
            assert receipt["status"] == 0, receipt["result"]
            # A 64-bit draw is beyond the 53-bit wire integers.
            assert receipt["result"] == {"__big_int__": str(expected)}
            assert receipt["state"] == [{"key": "con_rng.result", "value": expected}]
            driver.commit()
            assert driver.get("con_rng.result") == expected


@pytest.mark.parametrize("drop_second_seed", [False, True])
def test_live_rng_checks_compare_each_transaction_context(monkeypatch, drop_second_seed):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import localnet_contract_suite as checks

    signatures = ["signature-one", "signature-two"]
    values = [transaction_random_value(BLOCK, signature) for signature in signatures]
    sent = []
    for index, signature in enumerate(signatures):
        actual = values[0] if drop_second_seed else values[index]
        item = submission(result={"__big_int__": str(actual)})
        item.tx_hash = f"public-hash-{index}"
        item.receipt.transaction = {"metadata": {"signature": signature}}
        sent.append(item)
    client = SimpleNamespace(send_tx=AsyncMock(side_effect=sent))

    @asynccontextmanager
    async def connected(*args):
        yield client

    runner = SimpleNamespace(client=connected, nodes=list(range(5)))
    monkeypatch.setattr(checks, "matching_receipts", AsyncMock(return_value=17))
    monkeypatch.setattr(checks, "rpc", AsyncMock(return_value=BLOCK))
    state_checks = AsyncMock()
    monkeypatch.setattr(checks, "check_state", state_checks)
    expected = {}
    call = checks.transaction_random_checks(
        runner, None, {"commitments": "con_rng"}, None, expected
    )
    if drop_second_seed:
        with pytest.raises(E2EError, match="wrong result"):
            asyncio.run(call)
    else:
        observations = asyncio.run(call)
        assert len(observations) == client.send_tx.await_count == state_checks.await_count == 2
        assert expected == {"con_rng.result": values[1]}
        assert not any(signature in repr(observations) for signature in signatures)
