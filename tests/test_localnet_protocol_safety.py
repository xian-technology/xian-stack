from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

MODULE_PATH = Path(__file__).absolute().parents[1] / "scripts" / "localnet-protocol-safety.py"
sys.path.insert(0, str(MODULE_PATH.parent))

if "localnet_protocol_safety" in sys.modules:
    localnet_protocol_safety = sys.modules["localnet_protocol_safety"]
else:
    SPEC = importlib.util.spec_from_file_location(
        "localnet_protocol_safety",
        MODULE_PATH,
    )
    if SPEC is None or SPEC.loader is None:
        raise RuntimeError(f"unable to load {MODULE_PATH}")
    localnet_protocol_safety = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = localnet_protocol_safety
    SPEC.loader.exec_module(localnet_protocol_safety)


@pytest.mark.parametrize(
    ("paid", "chi_rate", "claimed", "error"),
    [
        (100, 20, True, None),
        (100, 1000, True, None),
        (99, 20, True, "unbond payout"),
        (99, 1000, True, "unbond payout"),
        (0, 1000, True, "unbond payout"),
        (100, 1000, False, "claimed flag"),
    ],
)
def test_unbond_claim_checks_full_payout_after_fees(paid, chi_rate, claimed, error):
    runner = object.__new__(localnet_protocol_safety.ProtocolSafetyRunner)
    charged_chi = 26_000
    receipt = {"chi_used": charged_chi, "tx_hash": "claim"}
    runner.submit_tx = AsyncMock(return_value=receipt)
    before, rate = Decimal("10000"), Decimal(chi_rate)
    after = before + paid - Decimal(charged_chi) / rate
    reader = SimpleNamespace(
        get_state=AsyncMock(side_effect=[before, rate, after]),
        call=AsyncMock(return_value={"claimed": claimed}),
    )
    unbond = {"owner": "delegator", "amount": 100}
    claim = runner.claim_pending_unbond(object(), reader, 3, unbond)
    if error:
        with pytest.raises(localnet_protocol_safety.RunnerError, match=error):
            asyncio.run(claim)
    else:
        result = asyncio.run(claim)
        assert result["claim_unbond"] == receipt
        assert result["claim_payout"]["amount"] == 100
        assert result["claim_payout"]["fee"] == Decimal(charged_chi) / rate
        assert result["claim_payout"]["balance_after"] == after
        assert reader.get_state.await_args.args == ("currency", "balances", "delegator")


class LocalnetProtocolSafetyTests(unittest.TestCase):
    def test_policy_approval_waits_for_effects_on_stale_primary(self):
        args = localnet_protocol_safety.build_parser().parse_args([])
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(localnet_protocol_safety, "OUTPUT_ROOT", Path(tmpdir)),
        ):
            runner = localnet_protocol_safety.ProtocolSafetyRunner(args)

        class Reader:
            def __init__(self, stale=False):
                self.reads = 0
                self.stale = stale

            async def get_state(self, *_args):
                self.reads += 1
                return "manual" if self.stale and self.reads == 1 else "auto_top_n"

        primary, peer = Reader(True), Reader()
        runner.submit_tx = AsyncMock(return_value={})
        runner.read_contract_state = AsyncMock(return_value=1)
        runner.read_members_vote = AsyncMock(
            side_effect=[{"status": "pending"}, {"status": "pending"}]
        )
        runner.wait_for_members_vote_progress = AsyncMock(return_value={"status": "approved"})
        result = asyncio.run(
            runner.approve_members_vote(
                primary,
                [("peer", peer)],
                type_of_vote="update_policy",
                arg={"selection_mode": "auto_top_n"},
                label_prefix="policy",
            )
        )
        self.assertEqual(result["proposal_final"]["status"], "approved")
        self.assertGreaterEqual(primary.reads, 2)
        self.assertGreaterEqual(peer.reads, 2)

    def test_receipt_keeps_the_commit_height_for_cross_node_reads(self):
        submission = SimpleNamespace(
            receipt=SimpleNamespace(
                execution={}, success=True, message="ok", raw={"result": {"height": "43"}}
            ),
            submitted=True,
            accepted=True,
            finalized=True,
            tx_hash="vote",
            nonce=0,
            chi_supplied=100,
        )
        receipt = localnet_protocol_safety.normalize_receipt(submission, label="unjail")
        self.assertEqual(receipt["height"], 43)

    def test_protocol_failure_keeps_original_error_and_writes_diagnostics_failure(self):
        import json

        args = localnet_protocol_safety.build_parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(localnet_protocol_safety, "OUTPUT_ROOT", Path(tmpdir)):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)
            runner.bootstrap = AsyncMock(side_effect=ValueError("original failure"))
            with patch.object(
                localnet_protocol_safety,
                "collect_failure_diagnostics",
                AsyncMock(side_effect=RuntimeError("Docker unavailable")),
            ):
                with self.assertRaisesRegex(ValueError, "original failure"):
                    localnet_protocol_safety.asyncio.run(runner.run())
            failure = json.loads((runner.output_dir / "00-bootstrap.json").read_text())
        self.assertFalse(failure["ok"])
        self.assertIn("original failure", failure["error"])
        self.assertIn("Docker unavailable", failure["diagnostics_error"])

    def test_vote_reader_waits_past_the_approving_block(self):
        args = localnet_protocol_safety.build_parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(localnet_protocol_safety, "OUTPUT_ROOT", Path(tmpdir)):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)
        node = self._node(0, "http://node-0")
        vote = {"vote_receipts": [{"height": 41}, {"height": 43}]}
        with (
            patch.object(
                localnet_protocol_safety, "latest_height", AsyncMock(side_effect=[41, 43, 44])
            ) as heights,
            patch.object(localnet_protocol_safety.asyncio, "sleep", AsyncMock()),
        ):
            result = localnet_protocol_safety.asyncio.run(
                runner.wait_for_vote_application(object(), node, vote)
            )
        self.assertEqual(result, 44)
        self.assertEqual(heights.await_count, 3)
        self.assertEqual(heights.await_args.args[1], "http://node-0")

    def test_vote_reader_rejects_missing_commit_evidence(self):
        runner = object.__new__(localnet_protocol_safety.ProtocolSafetyRunner)
        with self.assertRaisesRegex(localnet_protocol_safety.RunnerError, "receipt height"):
            localnet_protocol_safety.asyncio.run(
                runner.wait_for_vote_application(
                    object(), self._node(0, "http://node-0"), {"vote_receipts": []}
                )
            )

    def _node(self, index: int, rpc_url: str):
        return localnet_protocol_safety.LocalnetNode(
            index=index,
            moniker=f"node-{index}",
            rpc_url=rpc_url,
            rpc_port=27657 + index,
            p2p_port=27656 + index,
            metrics_port=27660 + index,
            abci_container=f"xian-node-{index}",
            cometbft_container=f"xian-node-{index}",
            account_public_key=f"pub-{index}",
            account_private_key=f"priv-{index}",
            bds_node=False,
        )

    def test_client_config_uses_localnet_rpc_timeout(self) -> None:
        args = localnet_protocol_safety.build_parser().parse_args(
            [
                "--rpc-timeout-seconds",
                "123",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_protocol_safety,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)

        config = runner.client_config()

        self.assertEqual(123, config.submission.timeout_seconds)
        self.assertEqual(0.5, config.submission.poll_interval_seconds)
        self.assertEqual(6, config.retry.max_attempts)

    def test_xian_client_owns_bounded_session(self) -> None:
        args = localnet_protocol_safety.build_parser().parse_args([])

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_protocol_safety,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)
        runner.network = {"chain_id": "test-chain"}
        runner.nodes = [self._node(0, "http://node-0")]

        client = runner.client(
            localnet_protocol_safety.Wallet(),
            0,
            session=object(),
        )

        self.assertIsNone(client._external_session)
        self.assertIsNone(client._session)
        self.assertEqual("http://node-0", client.node_url)
        self.assertEqual(
            runner.client_config().transport.total_timeout_seconds,
            client._timeout.total,
        )

    def test_recover_lagging_nodes_restarts_abci_unresponsive_node(self) -> None:
        args = localnet_protocol_safety.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_protocol_safety,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.restart_node_runtime = AsyncMock(
            return_value={"node": "node-1", "start": {"ready_height": 42}}
        )
        abci_attempts: dict[str, int] = {}

        async def fake_wait_for_height(_session, _rpc_url: str, _target_height: int, **_kwargs):
            return 42

        async def fake_wait_for_abci(_session, rpc_url: str, **_kwargs):
            abci_attempts[rpc_url] = abci_attempts.get(rpc_url, 0) + 1
            if rpc_url == "http://node-1" and abci_attempts[rpc_url] == 1:
                raise localnet_protocol_safety.RunnerError("ABCI query timed out")

        with (
            patch.object(
                localnet_protocol_safety,
                "wait_for_height",
                side_effect=fake_wait_for_height,
            ),
            patch.object(
                localnet_protocol_safety,
                "wait_for_abci_query_responsive",
                side_effect=fake_wait_for_abci,
            ),
        ):
            result = localnet_protocol_safety.asyncio.run(
                runner.recover_lagging_nodes(None, target_height=42, timeout_seconds=7)
            )

        runner.restart_node_runtime.assert_awaited_once_with(
            None,
            runner.nodes[1],
            target_height=42,
        )
        self.assertTrue(result["before"]["node-0"]["ok"])
        self.assertFalse(result["before"]["node-1"]["ok"])
        self.assertTrue(result["after"]["node-1"]["abci_query_ready"])

    def test_read_members_vote_falls_back_to_next_client(self) -> None:
        args = localnet_protocol_safety.build_parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_protocol_safety,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)

        class FailingClient:
            async def get_state(self, *_args):
                raise TimeoutError("stalled")

        class HealthyClient:
            async def get_state(self, *_args):
                return {"status": "approved"}

        result = localnet_protocol_safety.asyncio.run(
            runner.read_members_vote(
                [FailingClient(), HealthyClient()],
                proposal_id=1,
                timeout_seconds=1,
            )
        )

        self.assertEqual({"status": "approved"}, result)

    def test_read_members_vote_prefers_fresh_terminal_status(self) -> None:
        args = localnet_protocol_safety.build_parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_protocol_safety,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)

        class StaleClient:
            async def get_state(self, *_args):
                return {"status": "pending", "yes": 3, "yes_weight": 30}

        class FreshClient:
            async def get_state(self, *_args):
                return {"status": "approved", "yes": 4, "yes_weight": 42}

        result = localnet_protocol_safety.asyncio.run(
            runner.read_members_vote(
                [StaleClient(), FreshClient()],
                proposal_id=4,
                timeout_seconds=1,
            )
        )

        self.assertEqual("approved", result["status"])
        self.assertEqual(42, result["yes_weight"])

    def test_read_contract_state_falls_back_to_next_client(self) -> None:
        args = localnet_protocol_safety.build_parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_protocol_safety,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)

        class FailingClient:
            async def get_state(self, *_args):
                raise TimeoutError("stalled")

        class HealthyClient:
            async def get_state(self, *_args):
                return 7

        result = localnet_protocol_safety.asyncio.run(
            runner.read_contract_state(
                [FailingClient(), HealthyClient()],
                "validators",
                "total_votes",
                timeout_seconds=1,
            )
        )

        self.assertEqual(7, result)

    def test_governance_contract_call_stops_when_fallback_reader_sees_execution(
        self,
    ) -> None:
        args = localnet_protocol_safety.build_parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                localnet_protocol_safety,
                "OUTPUT_ROOT",
                Path(tmpdir),
            ):
                runner = localnet_protocol_safety.ProtocolSafetyRunner(args)

        state = {
            "proposal_id": 7,
            "status": "pending",
            "yes_votes": 0,
        }
        send_labels = []
        test_case = self

        def proposal_snapshot(*, stale: bool = False):
            if stale and state["status"] == "executed":
                return {
                    "proposal_id": state["proposal_id"],
                    "status": "pending",
                    "yes_votes": 3,
                    "no_votes": 0,
                    "yes_weight": 3,
                    "no_weight": 0,
                }
            return {
                "proposal_id": state["proposal_id"],
                "status": state["status"],
                "yes_votes": state["yes_votes"],
                "no_votes": 0,
                "yes_weight": state["yes_votes"],
                "no_weight": 0,
            }

        class StaleProposer:
            async def get_state(self, contract, variable):
                test_case.assertEqual(("governance", "proposal_count"), (contract, variable))
                return state["proposal_id"]

            async def call(self, contract, function, kwargs):
                test_case.assertEqual(("governance", "get_proposal"), (contract, function))
                test_case.assertEqual({"proposal_id": state["proposal_id"]}, kwargs)
                return proposal_snapshot(stale=True)

        class FreshReader:
            async def call(self, contract, function, kwargs):
                test_case.assertEqual(("governance", "get_proposal"), (contract, function))
                test_case.assertEqual({"proposal_id": state["proposal_id"]}, kwargs)
                return proposal_snapshot()

        async def fake_submit_tx(_client, contract, function, _kwargs, *, label, chi):
            self.assertEqual("governance", contract)
            self.assertEqual(localnet_protocol_safety.GOVERNANCE_TX_CHI, chi)
            send_labels.append(label)
            if function == "propose_contract_call":
                state["yes_votes"] = 1
            elif function == "vote":
                state["yes_votes"] += 1
                if state["yes_votes"] >= 4:
                    state["status"] = "executed"
            else:
                raise AssertionError(function)
            return {"label": label}

        runner.submit_tx = fake_submit_tx

        result = localnet_protocol_safety.asyncio.run(
            runner.approve_governance_contract_call(
                StaleProposer(),
                [
                    ("node1", FreshReader()),
                    ("node2", FreshReader()),
                    ("node3", FreshReader()),
                    ("node4", FreshReader()),
                ],
                target_contract="demo",
                target_function="set",
                kwargs={"value": 1},
                summary="demo update",
                label_prefix="governance-contract-call",
            )
        )

        self.assertEqual("executed", result["proposal_final"]["status"])
        self.assertEqual(3, len(result["vote_receipts"]))
        self.assertEqual(
            [
                "governance-contract-call-propose",
                "governance-contract-call-vote-1-node1",
                "governance-contract-call-vote-2-node2",
                "governance-contract-call-vote-3-node3",
            ],
            send_labels,
        )


if __name__ == "__main__":
    unittest.main()


def test_explicit_evidence_height_does_not_substitute_a_newer_block():
    assert localnet_protocol_safety.evidence_candidate_heights(100, 30, 7) == [7]
    assert localnet_protocol_safety.evidence_candidate_heights(4, 2, None) == [4, 3, 2]


@pytest.mark.parametrize("bad", [0, -1, True, 101, "7"])
def test_explicit_evidence_height_requires_a_committed_positive_integer(bad):
    with pytest.raises(localnet_protocol_safety.RunnerError, match="evidence height"):
        localnet_protocol_safety.evidence_candidate_heights(100, 30, bad)


@pytest.mark.parametrize("exact,expected", [(7, 7), (None, 99)])
def test_duplicate_vote_submission_is_signed_for_exact_or_first_available_committed_height(
    monkeypatch, exact, expected
):
    import base64
    import datetime
    import hashlib
    import json

    import nacl.signing
    from google.protobuf.internal.encoder import _VarintBytes
    from google.protobuf.timestamp_pb2 import Timestamp

    key = nacl.signing.SigningKey.generate()
    address = hashlib.sha256(bytes(key.verify_key)).digest()[:20].hex().upper()
    reporter = SimpleNamespace(rpc_url="http://reporter", moniker="reporter")
    target = SimpleNamespace(moniker="target", account_public_key="account")
    runner = object.__new__(localnet_protocol_safety.ProtocolSafetyRunner)
    runner.nodes = [reporter, target]
    runner.load_priv_validator_key = lambda index: {
        "address": address,
        "priv_key": {"value": base64.b64encode(bytes(key) + bytes(key.verify_key)).decode()},
    }
    monkeypatch.setattr(localnet_protocol_safety, "latest_height", AsyncMock(return_value=100))
    requested = []

    async def fetch(session, url, **kwargs):
        h = int(kwargs["params"]["height"])
        requested.append(h)
        if url.endswith("/validators"):
            entries = (
                [] if exact is None and h == 100 else [{"address": address, "voting_power": "10"}]
            )
            return {"result": {"validators": entries}}
        return {
            "result": {
                "block_id": {"hash": "AA" * 32, "parts": {"total": 1, "hash": "BB" * 32}},
                "block": {
                    "header": {
                        "height": str(h),
                        "time": "2026-11-16T00:00:00Z",
                        "chain_id": "unit-chain",
                    }
                },
            }
        }

    monkeypatch.setattr(localnet_protocol_safety, "fetch_json", fetch)

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"result": {"hash": "evidence-hash"}}

    submitted = []

    class Session:
        def get(self, url, **kwargs):
            submitted.append(json.loads(kwargs["params"]["evidence"]))
            return Response()

    result = asyncio.run(
        runner.broadcast_duplicate_vote_evidence(
            Session(),
            reporter_node_index=0,
            target_node_index=1,
            evidence_height=exact,
            height_lookback=2,
        )
    )
    assert result["height"] == expected
    assert requested == ([7, 7] if exact == 7 else [100, 100, 99, 99])
    assert submitted == [result["evidence"]]
    timestamp = Timestamp(
        seconds=int(datetime.datetime(2026, 11, 16, tzinfo=datetime.timezone.utc).timestamp())
    )
    value = result["evidence"]["value"]
    for vote in [value["vote_a"], value["vote_b"]]:
        assert vote["height"] == str(expected) and vote["validator_address"] == address
        canonical = localnet_protocol_safety.canonical_pb2.CanonicalVote(
            type=2,
            height=expected,
            round=0,
            chain_id="unit-chain",
            timestamp=timestamp,
            block_id=localnet_protocol_safety.canonical_pb2.CanonicalBlockID(
                hash=bytes.fromhex(vote["block_id"]["hash"]),
                part_set_header=localnet_protocol_safety.canonical_pb2.CanonicalPartSetHeader(
                    total=1, hash=bytes.fromhex(vote["block_id"]["parts"]["hash"])
                ),
            ),
        )
        payload = canonical.SerializeToString()
        key.verify_key.verify(
            _VarintBytes(len(payload)) + payload, base64.b64decode(vote["signature"])
        )
    assert {value["vote_a"]["block_id"]["hash"], value["vote_b"]["block_id"]["hash"]} == {
        "AA" * 32,
        "11" * 32,
    }


def test_duplicate_vote_helper_refuses_no_available_committed_validator_material(monkeypatch):
    import base64

    runner = object.__new__(localnet_protocol_safety.ProtocolSafetyRunner)
    runner.nodes = [SimpleNamespace(rpc_url="rpc", moniker="reporter"), SimpleNamespace()]
    runner.load_priv_validator_key = lambda index: {
        "address": "address",
        "priv_key": {"value": base64.b64encode(bytes(64)).decode()},
    }
    with pytest.raises(localnet_protocol_safety.RunnerError, match="could not find validator"):
        asyncio.run(
            runner.broadcast_duplicate_vote_evidence(
                object(),
                reporter_node_index=0,
                target_node_index=1,
                evidence_height=7,
                timeout_seconds=0,
            )
        )
