from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-protocol-safety.py"
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


class LocalnetProtocolSafetyTests(unittest.TestCase):
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
