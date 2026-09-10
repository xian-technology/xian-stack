"""Exercise proposal callbacks in an isolated process using the installed node runtime.

No connection to the running ABCI server or node database is opened. Live
query/admission checks are performed separately through CometBFT RPC.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

from abci import __version__
from cometbft.abci.v1beta3.types_pb2 import RequestPrepareProposal, RequestProcessProposal
from nacl.signing import SigningKey
from xian.fee_policy import TxFeePolicy
from xian.methods.prepare_proposal import prepare_proposal
from xian.methods.process_proposal import process_proposal
from xian.utils.encoding import encode_transaction_bytes
from xian.utils.tx import _native_decode_and_validate_transaction_static, canonical_json


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def probe(chain_id: str, expected_version: str) -> dict:
    require(__version__ == expected_version, "unexpected installed application version")
    config = tomllib.loads((Path.home() / ".cometbft/config/xian.toml").read_text())
    app = SimpleNamespace(
        chain_id=chain_id,
        max_tx_bytes=config.get("max_tx_bytes", 4 * 1024 * 1024),
        tx_fee_policy=TxFeePolicy.from_runtime_config(config),
        nonce_storage=SimpleNamespace(get_nonce=lambda _sender: None),
    )
    key = SigningKey.generate()
    sender = key.verify_key.encode().hex()

    def transaction(nonce=0, *, name=None, memo=None):
        payload = {
            "chain_id": chain_id,
            "contract": "currency" if name is None else "submission",
            "function": "transfer" if name is None else "submit_contract",
            "kwargs": {"amount": 1, "to": sender}
            if name is None
            else {
                "name": name,
                "code": "@export\ndef value():\n    return 1",
            },
            "nonce": nonce,
            "sender": sender,
            "chi_supplied": 100,
        }
        if memo is not None:
            payload["kwargs"]["memo"] = memo
        signature = key.sign(canonical_json(payload).encode()).signature.hex()
        return encode_transaction_bytes(
            canonical_json(
                {
                    "payload": payload,
                    "metadata": {"signature": signature},
                }
            )
        )

    txs = [transaction(0), transaction(1)]
    proposals = []
    for budget, expected in [(0, []), (len(txs[0]), txs[:1]), (sum(map(len, txs)), txs)]:
        response = await prepare_proposal(app, RequestPrepareProposal(txs=txs, max_tx_bytes=budget))
        require(list(response.txs) == expected, f"wrong proposal selection for budget {budget}")
        size = sum(map(len, response.txs))
        require(size <= budget, f"proposal exceeds byte budget: {size} > {budget}")
        accepted = await process_proposal(app, RequestProcessProposal(txs=response.txs))
        require(accepted.status == accepted.ACCEPT, "prepared proposal rejected by ProcessProposal")
        proposals.append({"budget": budget, "returned_bytes": size, "tx_count": len(response.txs)})

    skipped = await prepare_proposal(
        app,
        RequestPrepareProposal(
            txs=[transaction(0, memo="x" * 100), txs[1], txs[0]],
            max_tx_bytes=len(txs[0]),
        ),
    )
    require(list(skipped.txs) == txs[:1], "size-skipped transaction consumed proposal nonce")
    names = []
    invalid_txs = []
    for name, valid in [
        ("con_Uppercase", False),
        ("con_" + "a" * 61, False),
        ("con_" + "a" * 60, True),
        ("con_1", True),
    ]:
        tx = transaction(name=name)
        response = await prepare_proposal(
            app, RequestPrepareProposal(txs=[tx], max_tx_bytes=len(tx))
        )
        require(bool(response.txs) == valid, f"incorrect submission name admission: {name}")
        if not valid:
            invalid_txs.append({"name": name, "tx_hex": tx.hex()})
        names.append({"name": name, "accepted": valid})
    return {
        "application_version": __version__,
        "proposal_budgets": proposals,
        "skipped_nonce_preserved": True,
        "submission_names": names,
        "native_admission": _native_decode_and_validate_transaction_static is not None,
        "invalid_txs": invalid_txs,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe(sys.argv[1], sys.argv[2])), sort_keys=True))
