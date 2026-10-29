from __future__ import annotations

import asyncio
import base64
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import localnet_signature_checks as checks
from localnet_e2e_support import E2EError
from localnet_signature_checks import (
    genesis_timestamp,
    require_signature_rejection,
    signature_vectors,
)
from xian_py.wallet import Wallet, verify_msg


def test_signature_vectors_cover_valid_and_adversarial_ed25519():
    vectors = signature_vectors()
    assert {v["name"] for v in vectors} == {
        "valid",
        "identity",
        "noncanonical_scalar",
        "small_order_r",
        "mixed_order_r",
    }
    for vector in vectors:
        assert (
            verify_msg(vector["key"], vector["message"], vector["signature"]) is vector["expected"]
        )


@pytest.mark.parametrize("response", [{"code": 0}, {"code": 1, "log": "insufficient balance"}])
def test_admission_oracle_requires_signature_rejection(response):
    with pytest.raises(E2EError, match="signature"):
        require_signature_rejection(response, "node-0")


def test_genesis_timestamp_oracle_rejects_contract_time_drift():
    genesis = {
        "genesis_time": "2026-01-02T03:04:05.123456Z",
        "abci_genesis": {
            "genesis": [
                {
                    "key": "submission.__submitted__",
                    "value": {"__time__": [2026, 1, 2, 3, 4, 5, 123456]},
                },
                {
                    "key": "currency.__submitted__",
                    "value": {"__time__": [2026, 1, 2, 3, 4, 5, 123456]},
                },
            ]
        },
    }
    assert genesis_timestamp(genesis).isoformat() == "2026-01-02T03:04:05.123456+00:00"
    drift = copy.deepcopy(genesis)
    drift["abci_genesis"]["genesis"][1]["value"]["__time__"][-1] = 0
    with pytest.raises(E2EError, match="currency.__submitted__"):
        genesis_timestamp(drift)
    genesis["abci_genesis"]["genesis"] = []
    with pytest.raises(E2EError, match="submission timestamp"):
        genesis_timestamp(genesis)


def test_admission_checks_both_attacks_on_every_validator(monkeypatch):
    runner = SimpleNamespace(
        nodes=[SimpleNamespace(moniker=f"node-{i}") for i in range(5)],
        founder_wallet=Wallet("22" * 32),
        network={"chain_id": "local-test"},
    )
    submit = AsyncMock(return_value={"code": 1, "log": "Bad signature"})
    monkeypatch.setattr(checks, "broadcast", submit)
    report = asyncio.run(checks.check_admission(runner, object()))
    assert len(report) == 10
    assert {item["node"] for item in report} == {f"node-{i}" for i in range(5)}
    assert len({item["tx_hash"] for item in report}) == 10
    for call in submit.await_args_list:
        assert call.kwargs == {"accepted": False}


@pytest.mark.parametrize("corrupt", [False, True])
def test_live_genesis_time_oracle_checks_committed_contract_metadata(
    tmp_path, monkeypatch, corrupt
):
    node = SimpleNamespace(moniker="node-0")
    runner = SimpleNamespace(nodes=[node], network={"chain_id": "local-test"})
    genesis = {
        "chain_id": "local-test",
        "genesis_time": "2026-01-02T03:04:05.123456Z",
        "abci_genesis": {
            "genesis": [
                {
                    "key": "submission.__submitted__",
                    "value": {"__time__": [2026, 1, 2, 3, 4, 5, 123456]},
                }
            ]
        },
    }
    path = tmp_path / ".localnet/node-0/.cometbft/config/genesis.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(genesis))
    monkeypatch.setattr(checks, "STACK", tmp_path)
    text = "2026-01-02 03:04:05.123456" if not corrupt else "2026-01-02 03:04:05"
    response = AsyncMock(
        return_value={"response": {"code": 0, "value": base64.b64encode(text.encode()).decode()}}
    )
    monkeypatch.setattr(checks, "rpc", response)
    if corrupt:
        with pytest.raises(E2EError, match="submission time drift"):
            asyncio.run(checks.check_genesis_times(runner, object()))
    else:
        assert (
            asyncio.run(checks.check_genesis_times(runner, object()))
            == "2026-01-02T03:04:05.123456+00:00"
        )
        assert response.await_count == 3


@pytest.mark.parametrize("corrupt", [False, True])
def test_signature_phase_requires_committed_verification_results(monkeypatch, corrupt):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def send_tx(self, *_args, **kwargs):
            assert kwargs["wait_for_tx"]
            return SimpleNamespace(
                finalized=True,
                receipt=SimpleNamespace(success=True),
                message="",
                tx_hash="committed",
            )

    runner = SimpleNamespace(
        nodes=[SimpleNamespace(moniker="node-0")],
        founder_wallet=Wallet("22" * 32),
        client=lambda *_: Client(),
    )
    monkeypatch.setattr(checks, "check_genesis_times", AsyncMock(return_value="time"))
    monkeypatch.setattr(checks, "check_admission", AsyncMock(return_value=[]))
    monkeypatch.setattr(checks, "deploy", AsyncMock(return_value="con_probe"))
    monkeypatch.setattr(checks, "agreement", AsyncMock(return_value={"ok": True}))
    values = [True, corrupt, False, False, False]
    monkeypatch.setattr(checks, "query", AsyncMock(return_value=values))
    if corrupt:
        with pytest.raises(E2EError, match="VM signature acceptance drift"):
            asyncio.run(checks.signature_phase(runner, object()))
    else:
        assert asyncio.run(checks.signature_phase(runner, object()))["tx_hash"] == "committed"
