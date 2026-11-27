"""Smoke-test proposal callbacks and logging in the installed node image."""

from __future__ import annotations

import asyncio
import json
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

from abci import __version__
from loguru import logger
from nacl.signing import SigningKey
from tendermint.abci.types_pb2 import RequestPrepareProposal, RequestProcessProposal
from xian.app_logging import configure_logging
from xian.fee_policy import TxFeePolicy, resolve_chain_fee_policy
from xian.methods.prepare_proposal import prepare_proposal
from xian.methods.process_proposal import process_proposal
from xian.utils import tx as tx_utils
from xian.utils.encoding import encode_transaction_bytes
from xian.utils.tx import canonical_json


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def chain_fee_policy(home: Path) -> TxFeePolicy:
    """The chain fee policy is genesis state; node config cannot override it."""
    genesis = json.loads((home / "config/genesis.json").read_text())
    return resolve_chain_fee_policy(committed_value=None, genesis=genesis)


async def probe(chain_id: str, expected_version: str) -> dict:
    require(__version__ == expected_version, "unexpected installed application version")
    home = Path.home() / ".cometbft"
    config = tomllib.loads((home / "config/xian.toml").read_text())
    cache_factory = getattr(tx_utils, "create_transaction_admission_cache", None)
    app = SimpleNamespace(
        chain_id=chain_id,
        max_tx_bytes=config.get("max_tx_bytes", 4 * 1024 * 1024),
        tx_fee_policy=chain_fee_policy(home),
        nonce_storage=SimpleNamespace(get_nonce=lambda _sender: None),
        transaction_admission_cache=cache_factory(chain_id) if cache_factory else None,
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
    require(list(skipped.txs) == txs[1:], "proposal did not include the higher nonce that fits")
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
        "higher_nonce_selected": True,
        "submission_names": names,
        "invalid_txs": invalid_txs,
    }


def check_image_logging(level: str, structured: bool) -> None:
    with tempfile.TemporaryDirectory() as storage:
        settings = configure_logging(
            SimpleNamespace(STORAGE_HOME=Path(storage)),
            {"app_log_level": level, "app_log_json": structured},
        )
        try:
            logger.log(level, "Node image logging probe")
            logger.complete()
        finally:
            logger.remove()
        lines = (Path(settings["logs_dir"]) / "xian-abci.log").read_text().splitlines()
        if structured:
            record = json.loads(lines[-1])["record"]
            require(record["level"]["name"] == level, "Wrong structured log level")
            message = record["message"]
        else:
            require(level in lines[-1], "Wrong plain-text log level")
            message = lines[-1]
        require("Node image logging probe" in message, "Missing application log probe")


def image_smoke() -> None:
    genesis = json.loads((Path.home() / ".cometbft/config/genesis.json").read_text())
    with urlopen("http://127.0.0.1:26657/abci_info", timeout=5) as response:
        live_version = json.load(response)["result"]["response"]["version"]
    asyncio.run(probe(genesis["chain_id"], live_version))
    check_image_logging("DEBUG", structured=False)
    check_image_logging("TRACE", structured=True)
    print("Node image proposal callbacks and logging passed")


if __name__ == "__main__":
    image_smoke()
