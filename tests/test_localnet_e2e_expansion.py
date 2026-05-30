from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-e2e.py"
sys.path.insert(0, str(MODULE_PATH.parent))

localnet_e2e_phases = importlib.import_module("localnet_e2e_phases")

if "localnet_e2e" in sys.modules:
    localnet_e2e = sys.modules["localnet_e2e"]
else:
    SPEC = importlib.util.spec_from_file_location("localnet_e2e", MODULE_PATH)
    if SPEC is None or SPEC.loader is None:
        raise RuntimeError(f"unable to load {MODULE_PATH}")
    localnet_e2e = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = localnet_e2e
    SPEC.loader.exec_module(localnet_e2e)


class LocalnetE2EExpansionTests(unittest.TestCase):
    @staticmethod
    def _node(
        index: int,
        rpc_url: str,
        *,
        bds_node: bool = False,
    ) -> localnet_e2e.LocalnetNode:
        return localnet_e2e.LocalnetNode(
            index=index,
            moniker=f"node-{index}",
            rpc_url=rpc_url,
            rpc_port=26657 + index,
            p2p_port=26656 + index,
            metrics_port=26660 + index,
            abci_container=f"xian-node-{index}",
            cometbft_container=f"xian-node-{index}",
            account_public_key=f"pub-{index}",
            account_private_key=f"priv-{index}",
            bds_node=bds_node,
        )

    def test_atomic_and_throughput_phases_are_in_five_node_sequence(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()

        self.assertLess(
            phase_names.index("03-contract-orchestration"),
            phase_names.index("03-atomic-rollback"),
        )
        self.assertLess(
            phase_names.index("03-atomic-rollback"),
            phase_names.index("03-x402-exact"),
        )
        self.assertEqual(
            "08-throughput-mix",
            phase_names[phase_names.index("07-dex-mixed") + 1],
        )
        self.assertEqual(
            "08-simulator-load",
            phase_names[phase_names.index("08-throughput-mix") + 1],
        )

    def test_phase_registry_is_separate_from_runner_execution(self) -> None:
        self.assertEqual(
            localnet_e2e_phases.phase_names(),
            localnet_e2e.E2ERunner.phase_names(),
        )
        self.assertEqual(
            {
                "05-burst-load",
                "07-dex-mixed",
                "08-throughput-mix",
            },
            {phase.name for phase in localnet_e2e_phases.PHASE_SPECS if not phase.uses_session},
        )
        for phase in localnet_e2e_phases.PHASE_SPECS:
            self.assertTrue(
                hasattr(localnet_e2e.E2ERunner, phase.method_name),
                msg=f"missing phase method for {phase.name}: {phase.method_name}",
            )

    def test_cli_exposes_throughput_mix_sizing_knobs(self) -> None:
        args = localnet_e2e.build_parser().parse_args(
            [
                "--start-phase",
                "08-throughput-mix",
                "--resume-dir",
                "/tmp/localnet-e2e",
                "--no-bootstrap",
                "--no-build",
                "--transfer-fanout-ops",
                "12",
                "--contract-heavy-ops",
                "7",
                "--throughput-wallet-count",
                "3",
                "--throughput-submit-workers",
                "4",
                "--contract-heavy-rounds",
                "5",
            ]
        )

        self.assertEqual("08-throughput-mix", args.start_phase)
        self.assertEqual(12, args.transfer_fanout_ops)
        self.assertEqual(7, args.contract_heavy_ops)
        self.assertEqual(3, args.throughput_wallet_count)
        self.assertEqual(4, args.throughput_submit_workers)
        self.assertEqual(5, args.contract_heavy_rounds)

    def test_default_client_config_uses_localnet_rpc_timeout(self) -> None:
        args = localnet_e2e.build_parser().parse_args(
            [
                "--rpc-timeout-seconds",
                "123",
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)

        config = runner.default_client_config()

        self.assertEqual(123, config.submission.timeout_seconds)
        self.assertEqual(0.5, config.submission.poll_interval_seconds)

    def test_latest_heights_best_effort_keeps_partial_statuses(self) -> None:
        nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
            self._node(2, "http://node-2"),
        ]

        async def fake_latest_height(_session, rpc_url: str) -> int:
            if rpc_url.endswith("node-1"):
                raise TimeoutError("node is wedged")
            return {"http://node-0": 41, "http://node-2": 39}[rpc_url]

        with patch.object(localnet_e2e, "latest_height", side_effect=fake_latest_height):
            statuses = asyncio.run(localnet_e2e.latest_heights_best_effort(None, nodes))

        self.assertEqual({"ok": True, "height": 41}, statuses["node-0"])
        self.assertEqual({"ok": True, "height": 39}, statuses["node-2"])
        self.assertFalse(statuses["node-1"]["ok"])
        self.assertIn("TimeoutError", statuses["node-1"]["error"])

    def test_stabilize_nodes_recovers_to_highest_observed_height(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.recover_lagging_nodes = AsyncMock(return_value={"restarts": []})
        statuses = {
            "node-0": {"ok": True, "height": 12},
            "node-1": {"ok": False, "error": "TimeoutError: node is wedged"},
        }

        with patch.object(localnet_e2e, "latest_heights_best_effort", return_value=statuses):
            result = asyncio.run(
                runner.stabilize_nodes(
                    None,
                    reason="unit test",
                    timeout_seconds=7,
                    min_target_height=10,
                )
            )

        runner.recover_lagging_nodes.assert_awaited_once_with(
            None,
            target_height=12,
            timeout_seconds=7,
        )
        self.assertEqual("unit test", result["reason"])
        self.assertEqual(12, result["target_height"])
        self.assertEqual(0, result["advance_blocks"])
        self.assertEqual(statuses, result["snapshot"])

    def test_stabilize_nodes_can_require_height_progress(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.recover_lagging_nodes = AsyncMock(return_value={"restarts": []})
        statuses = {
            "node-0": {"ok": True, "height": 12},
            "node-1": {"ok": True, "height": 12},
        }

        with patch.object(localnet_e2e, "latest_heights_best_effort", return_value=statuses):
            result = asyncio.run(
                runner.stabilize_nodes(
                    None,
                    reason="phase boundary",
                    timeout_seconds=7,
                    advance_blocks=1,
                )
            )

        runner.recover_lagging_nodes.assert_awaited_once_with(
            None,
            target_height=13,
            timeout_seconds=7,
        )
        self.assertEqual(13, result["target_height"])
        self.assertEqual(1, result["advance_blocks"])

    def test_healthy_submission_node_skips_bds_and_lagging_nodes(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0", bds_node=True),
            self._node(1, "http://node-1"),
            self._node(2, "http://node-2"),
        ]

        async def fake_fetch_json(_session, url: str, *, timeout: float):
            heights = {
                "http://node-0/status": 50,
                "http://node-1/status": 10,
                "http://node-2/status": 49,
            }
            return {
                "result": {
                    "sync_info": {
                        "latest_block_height": str(heights[url]),
                        "catching_up": False,
                    }
                }
            }

        with patch.object(localnet_e2e, "fetch_json", side_effect=fake_fetch_json):
            selected = asyncio.run(
                runner.healthy_submission_node_index(None, preferred_index=0)
            )

        self.assertEqual(2, selected)

    def test_uniform_state_wait_recovers_nodes_before_retry(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.stabilize_nodes = AsyncMock(return_value={"recovery": {"restarts": []}})

        with patch.object(
            localnet_e2e,
            "wait_for_uniform_node_state",
            AsyncMock(
                side_effect=[
                    localnet_e2e.E2EError("node-1 timed out"),
                    {"node-0": "ready", "node-1": "ready"},
                ]
            ),
        ) as wait_for_state:
            result = asyncio.run(
                runner.wait_for_uniform_node_state(
                    None,
                    runner.nodes,
                    contract="con_demo",
                    variable="value",
                    expected="ready",
                    label="demo state",
                    timeout_seconds=10,
                )
            )

        self.assertEqual({"node-0": "ready", "node-1": "ready"}, result)
        runner.stabilize_nodes.assert_awaited_once_with(
            None,
            reason="while waiting for demo state",
            timeout_seconds=10.0,
            advance_blocks=1,
        )
        self.assertEqual(2, wait_for_state.await_count)

    def test_fund_wallets_uses_checktx_with_receipt_polling(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [self._node(0, "http://node-0")]
        runner.founder_wallet = SimpleNamespace(public_key="founder")
        runner.network = {"chain_id": "test-chain"}
        runner.stabilize_nodes = AsyncMock(return_value={"recovery": {"restarts": []}})
        runner.wait_for_uniform_node_state = AsyncMock(return_value={"node-0": "3000"})
        send_calls = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def send(self, **kwargs):
                send_calls.append(kwargs)
                return SimpleNamespace(
                    submitted=True,
                    accepted=True,
                    finalized=True,
                    message=None,
                    tx_hash="tx-hash",
                    mode=kwargs["mode"],
                    nonce=1,
                    chi_supplied=kwargs["chi"],
                    receipt=SimpleNamespace(
                        success=True,
                        message="ok",
                        execution={"state": [], "events": [], "chi_used": 10},
                    ),
                )

        runner.client = lambda *_args, **_kwargs: FakeClient()
        wallet = SimpleNamespace(public_key="wallet-0")

        with patch.object(localnet_e2e, "fetch_abci_query", AsyncMock(return_value="0")):
            receipts = asyncio.run(runner.fund_wallets(None, [wallet], amount=3_000))

        self.assertEqual(1, len(receipts))
        self.assertEqual("checktx", send_calls[0]["mode"])
        self.assertTrue(send_calls[0]["wait_for_tx"])


if __name__ == "__main__":
    unittest.main()
