"""Live Ed25519 rejection parity and genesis timestamp consistency checks."""

from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime

from localnet_chain_checks import STACK, agreement, broadcast, deploy, query, require, rpc
from xian_py import transaction
from xian_py.wallet import Wallet

MESSAGE = "xian parity test"
IDENTITY = "01" + "00" * 31
PROBE_SOURCE = """
results = Variable()
@export
def probe(vectors: list):
    checked = []
    for vector in vectors:
        checked.append(crypto.verify(vector['key'], vector['message'], vector['signature']))
    results.set(checked)
    return checked
"""


def noncanonical_scalar(signature):
    raw = bytes.fromhex(signature)
    order = 2**252 + 27742317777372353535851937790883648493
    scalar = int.from_bytes(raw[32:], "little") + order
    return (raw[:32] + scalar.to_bytes(32, "little")).hex()


def signature_vectors():
    signer = Wallet("11" * 32)
    valid = signer.sign_msg(MESSAGE)
    cases = [
        ("valid", signer.public_key, valid, True),
        ("identity", IDENTITY, IDENTITY + "00" * 32, False),
        ("noncanonical_scalar", signer.public_key, noncanonical_scalar(valid), False),
        (
            "small_order_r",
            signer.public_key,
            "0100000000000000000000000000000000000000000000000000000000000000"
            "edaf87719b1f81c47646a414a04a0710b9c7ff5918a34e01e53c30bb2e2f5f00",
            False,
        ),
        (
            "mixed_order_r",
            signer.public_key,
            "5252cc0a7f208133b620acbd4537eba2a4123bf0a8c2e4f980c3b31bb69765ea"
            "7e38ee9f538979938c47d1e0f5577a489466ef86883378ba092d0b1de2f59307",
            False,
        ),
    ]
    return [
        dict(name=name, key=key, message=MESSAGE, signature=sig, expected=expected)
        for name, key, sig, expected in cases
    ]


def genesis_timestamp(genesis):
    timestamp = datetime.fromisoformat(genesis["genesis_time"].replace("Z", "+00:00"))
    require(timestamp.tzinfo is not None, "Genesis time has no timezone")
    timestamp = timestamp.astimezone(UTC)
    expected = {
        "__time__": [
            timestamp.year,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
            timestamp.microsecond,
        ]
    }
    submitted = [
        entry
        for entry in genesis["abci_genesis"]["genesis"]
        if entry["key"].endswith(".__submitted__")
    ]
    require(bool(submitted), "Genesis has no contract submission timestamps")
    for entry in submitted:
        require(entry["value"] == expected, f"Genesis timestamp drift: {entry['key']}")
    return timestamp


async def check_genesis_times(runner, session):
    timestamps = []
    for node in runner.nodes:
        path = STACK / ".localnet" / node.moniker / ".cometbft" / "config" / "genesis.json"
        genesis = json.loads(path.read_text())
        require(genesis["chain_id"] == runner.network["chain_id"], "Wrong genesis chain")
        timestamp = genesis_timestamp(genesis)
        timestamps.append(timestamp.isoformat())
        for name in ("submission", "currency", "validators"):
            response = (
                await rpc(
                    session, node, "abci_query", path=json.dumps(f"/get/{name}.__submitted__")
                )
            )["response"]
            require(int(response.get("code", 0)) == 0, f"Missing {name} submission time")
            text = base64.b64decode(response.get("value") or "").decode()
            actual = datetime.fromisoformat(text).replace(tzinfo=UTC)
            require(actual == timestamp, f"{node.moniker}: {name} submission time drift")
    require(len(set(timestamps)) == 1, "Validators have different genesis timestamps")
    return timestamps[0]


def require_signature_rejection(result, moniker):
    require(
        int(result.get("code", 0)) != 0 and "signature" in result.get("log", "").lower(),
        f"{moniker}: expected signature rejection, received {result}",
    )


async def check_admission(runner, session):
    rejected = []
    for index, node in enumerate(runner.nodes):
        tx = transaction.create_tx(
            {
                "chain_id": runner.network["chain_id"],
                "sender": runner.founder_wallet.public_key,
                "nonce": index,
                "contract": "currency",
                "function": "transfer",
                "kwargs": {"to": runner.founder_wallet.public_key, "amount": 1},
                "chi_supplied": 2000,
            },
            runner.founder_wallet,
        )
        scalar = copy.deepcopy(tx)
        scalar["metadata"]["signature"] = noncanonical_scalar(tx["metadata"]["signature"])
        identity = copy.deepcopy(tx)
        identity["payload"]["sender"] = IDENTITY
        identity["metadata"]["signature"] = IDENTITY + "00" * 32
        for label, invalid in (("identity", identity), ("noncanonical_scalar", scalar)):
            prepared = transaction.prepare_transaction(invalid)
            result = await broadcast(session, node, prepared, accepted=False)
            require_signature_rejection(result, node.moniker)
            rejected.append({"node": node.moniker, "case": label, "tx_hash": prepared.tx_hash})
    return rejected


async def signature_phase(runner, session):
    genesis_time = await check_genesis_times(runner, session)
    rejected = await check_admission(runner, session)
    contract = await deploy(runner, session, "ed25519-verification", PROBE_SOURCE)
    vectors = signature_vectors()
    async with runner.client(runner.founder_wallet, 0, session) as client:
        result = await client.send_tx(
            contract, "probe", {"vectors": vectors}, chi=100_000, wait_for_tx=True
        )
        require(
            result.finalized and result.receipt.success,
            f"Signature verification fixture failed: {result.message}",
        )
    consensus = await agreement(session, runner.nodes, window=1)
    for node in runner.nodes:
        actual = await query(session, node, f"/get/{contract}.results")
        require(
            actual == [v["expected"] for v in vectors],
            f"{node.moniker}: VM signature acceptance drift: {actual}",
        )
    return {
        "genesis_time": genesis_time,
        "rejected": rejected,
        "vm_cases": [v["name"] for v in vectors],
        "tx_hash": result.tx_hash,
        "consensus": consensus,
    }
