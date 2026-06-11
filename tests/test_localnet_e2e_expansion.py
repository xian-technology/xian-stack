from __future__ import annotations

import asyncio
import importlib.util
import inspect
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
    def test_nested_uv_commands_preserve_patch_python_version(self) -> None:
        expected = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

        self.assertEqual(localnet_e2e.CURRENT_UV_PYTHON, expected)

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

    def test_orchestration_phase_covers_pending_callback_overlay(self) -> None:
        workload_dir = Path(__file__).resolve().parents[1] / "workloads" / "e2e"
        controller_source = (workload_dir / "pending_overlay_controller.py").read_text()
        adapter_source = (workload_dir / "pending_overlay_adapter.py").read_text()
        phase_source = inspect.getsource(localnet_e2e.E2ERunner.contract_orchestration_phase)

        self.assertIn("adapter_spend_public", controller_source)
        self.assertIn("get_active_public_spend_remaining", adapter_source)
        self.assertIn("adapter did not see the controller's pending spend budget", adapter_source)
        self.assertIn("pending_overlay_path", phase_source)
        self.assertIn("orchestration-pending-overlay", phase_source)

    def test_validator_governance_phase_covers_selection_mode_switches(self) -> None:
        phase_source = inspect.getsource(localnet_e2e.E2ERunner.validator_governance_phase)

        self.assertIn('"selection_mode": "auto_top_n"', phase_source)
        self.assertIn('"selection_mode": "hybrid"', phase_source)
        self.assertIn("hybrid-rebalance-before-standby-approval", phase_source)
        self.assertIn("hybrid mode admitted a registered candidate", phase_source)
        self.assertIn('"selection_mode": "manual"', phase_source)

    def test_shielded_phase_promotes_bundles_before_governance_binding(self) -> None:
        phase_source = inspect.getsource(localnet_e2e.E2ERunner.shielded_phase)

        self.assertIn("xian-zk-shielded-bundle", phase_source)
        self.assertIn('"promote"', phase_source)
        self.assertIn('"ceremony-import"', phase_source)
        self.assertIn("shielded-note-registry-manifest.json", phase_source)
        self.assertIn("shielded-relay-registry-manifest.json", phase_source)
        self.assertIn("catalog-artifacts-snippet.json", phase_source)
        self.assertIn("register_and_bind.py", phase_source)
        self.assertIn("configure_vk", phase_source)

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

    def test_finalize_summary_includes_phase_stabilizations(self) -> None:
        args = localnet_e2e.build_parser().parse_args([])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.phase_stabilizations = [
            {
                "phase": "02-xian-py-smoke",
                "recovery": {
                    "lagging": [],
                    "restarts": [],
                },
            }
        ]

        summary = asyncio.run(runner.finalize_summary())

        self.assertEqual(runner.phase_stabilizations, summary["phase_stabilizations"])

    def test_normalize_receipt_keeps_lookup_height(self) -> None:
        submission = SimpleNamespace(
            submitted=True,
            accepted=True,
            finalized=True,
            message=None,
            tx_hash="tx-hash",
            nonce=7,
            chi_supplied=123,
            receipt=SimpleNamespace(
                success=True,
                message=None,
                execution={"state": [], "events": [], "chi_used": 11},
                raw={"result": {"height": "42", "index": "3"}},
            ),
        )

        receipt = localnet_e2e.normalize_receipt(submission, label="demo")

        self.assertEqual(42, receipt["height"])
        self.assertEqual(3, receipt["tx_index"])

    def test_max_receipt_height_uses_lagged_transaction_heights(self) -> None:
        self.assertEqual(
            57,
            localnet_e2e.max_receipt_height(
                [{"height": "41"}, {"height": 57}, {"height": None}],
                fallback=50,
            ),
        )

    def test_workload_subprocess_inherits_localnet_rpc_timeout(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "123"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        captured_cmd = []

        def fake_run_cmd(cmd, *, cwd):
            captured_cmd.extend(cmd)
            return SimpleNamespace(stdout='{"scenario":"transfer_fanout","ok":true}')

        with patch.object(localnet_e2e, "run_cmd", side_effect=fake_run_cmd):
            asyncio.run(runner.run_localnet_workload(scenario="transfer_fanout"))

        timeout_flag_index = captured_cmd.index("--receipt-timeout-seconds")
        self.assertEqual("123.0", captured_cmd[timeout_flag_index + 1])

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

    def test_wait_for_height_requires_catchup_complete(self) -> None:
        responses = [
            {
                "result": {
                    "sync_info": {
                        "latest_block_height": "42",
                        "catching_up": True,
                    }
                }
            },
            {
                "result": {
                    "sync_info": {
                        "latest_block_height": "42",
                        "catching_up": False,
                    }
                }
            },
        ]

        async def fake_fetch_json(_session, _url: str, *, timeout: float):
            return responses.pop(0)

        with (
            patch.object(localnet_e2e, "fetch_json", side_effect=fake_fetch_json),
            patch.object(localnet_e2e.asyncio, "sleep", AsyncMock()),
        ):
            height = asyncio.run(
                localnet_e2e.wait_for_height(
                    None,
                    "http://node-1",
                    42,
                    timeout_seconds=5,
                )
            )

        self.assertEqual(42, height)
        self.assertEqual([], responses)

    def test_stabilize_nodes_can_recover_to_highest_observed_height(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.recover_lagging_nodes = AsyncMock(return_value={"lagging": [], "restarts": []})
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
                    allow_restarts=True,
                )
            )

        runner.recover_lagging_nodes.assert_awaited_once_with(
            None,
            target_height=12,
            timeout_seconds=7,
            restart_lagging=True,
        )
        self.assertEqual("unit test", result["reason"])
        self.assertEqual(12, result["target_height"])
        self.assertEqual(0, result["advance_blocks"])
        self.assertTrue(result["allow_restarts"])
        self.assertEqual(statuses, result["snapshot"])

    def test_stabilize_nodes_fails_lagging_nodes_without_restart(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.recover_lagging_nodes = AsyncMock(
            return_value={
                "lagging": [{"node": "node-1", "error": "TimeoutError: node is wedged"}],
                "restarts": [],
            }
        )
        statuses = {
            "node-0": {"ok": True, "height": 12},
            "node-1": {"ok": False, "error": "TimeoutError: node is wedged"},
        }

        with (
            patch.object(localnet_e2e, "latest_heights_best_effort", return_value=statuses),
            self.assertRaisesRegex(localnet_e2e.E2EError, "without restart"),
        ):
            asyncio.run(
                runner.stabilize_nodes(
                    None,
                    reason="phase boundary",
                    timeout_seconds=7,
                )
            )

        runner.recover_lagging_nodes.assert_awaited_once_with(
            None,
            target_height=12,
            timeout_seconds=7,
            restart_lagging=False,
        )

    def test_stabilize_nodes_can_require_height_progress(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.recover_lagging_nodes = AsyncMock(return_value={"lagging": [], "restarts": []})
        statuses = {
            "node-0": {"ok": True, "height": 12},
            "node-1": {"ok": True, "height": 11},
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
            restart_lagging=False,
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

        with (
            patch.object(localnet_e2e, "fetch_json", side_effect=fake_fetch_json),
            patch.object(localnet_e2e, "abci_query_responsive", AsyncMock(return_value=True)),
        ):
            selected = asyncio.run(runner.healthy_submission_node_index(None, preferred_index=0))

        self.assertEqual(2, selected)

    def test_healthy_submission_node_does_not_probe_bds_abci(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0", bds_node=True),
            self._node(1, "http://node-1"),
            self._node(2, "http://node-2"),
        ]
        status_urls = []
        abci_urls = []

        async def fake_fetch_json(_session, url: str, *, timeout: float):
            status_urls.append(url)
            return {
                "result": {
                    "sync_info": {
                        "latest_block_height": "100",
                        "catching_up": False,
                    }
                }
            }

        async def fake_abci_query_responsive(_session, rpc_url: str, *, timeout: float = 2.0):
            abci_urls.append(rpc_url)
            return True

        with (
            patch.object(localnet_e2e, "fetch_json", side_effect=fake_fetch_json),
            patch.object(
                localnet_e2e,
                "abci_query_responsive",
                side_effect=fake_abci_query_responsive,
            ),
        ):
            selected = asyncio.run(runner.healthy_submission_node_index(None, preferred_index=0))

        self.assertEqual(1, selected)
        self.assertEqual(["http://node-1/status", "http://node-2/status"], status_urls)
        self.assertEqual(["http://node-1", "http://node-2"], abci_urls)

    def test_healthy_submission_node_skips_unresponsive_abci_nodes(self) -> None:
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
                "http://node-0/status": 80,
                "http://node-1/status": 80,
                "http://node-2/status": 79,
            }
            return {
                "result": {
                    "sync_info": {
                        "latest_block_height": str(heights[url]),
                        "catching_up": False,
                    }
                }
            }

        async def fake_abci_query_responsive(_session, rpc_url: str, *, timeout: float = 2.0):
            return rpc_url == "http://node-2"

        with (
            patch.object(localnet_e2e, "fetch_json", side_effect=fake_fetch_json),
            patch.object(
                localnet_e2e,
                "abci_query_responsive",
                side_effect=fake_abci_query_responsive,
            ),
        ):
            selected = asyncio.run(runner.healthy_submission_node_index(None, preferred_index=1))

        self.assertEqual(2, selected)

    def test_healthy_submission_node_honors_excluded_indices(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0", bds_node=True),
            self._node(1, "http://node-1"),
            self._node(2, "http://node-2"),
        ]

        async def fake_fetch_json(_session, _url: str, *, timeout: float):
            return {
                "result": {
                    "sync_info": {
                        "latest_block_height": "90",
                        "catching_up": False,
                    }
                }
            }

        with (
            patch.object(localnet_e2e, "fetch_json", side_effect=fake_fetch_json),
            patch.object(localnet_e2e, "abci_query_responsive", AsyncMock(return_value=True)),
        ):
            selected = asyncio.run(
                runner.healthy_submission_node_index(
                    None,
                    preferred_index=1,
                    excluded_indices={1},
                )
            )

        self.assertEqual(2, selected)

    def test_next_nonce_with_rpc_failover_retries_on_read_timeout(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0", bds_node=True),
            self._node(1, "http://node-1"),
            self._node(2, "http://node-2"),
        ]
        runner.healthy_submission_node_index = AsyncMock(side_effect=[1, 2])
        wallet = SimpleNamespace(public_key="wallet-0")
        refresh_attempts = []

        class FakeClient:
            def __init__(self, node_index: int):
                self.node_index = node_index

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def refresh_nonce(self):
                refresh_attempts.append(self.node_index)
                if self.node_index == 1:
                    raise TimeoutError("nonce read stalled")
                return 17

        def fake_client(_wallet, node_index: int, _session):
            return FakeClient(node_index)

        runner.client = fake_client

        nonce, node_index = asyncio.run(
            runner.next_nonce_with_rpc_failover(
                None,
                wallet,
                preferred_index=1,
                excluded_indices={0},
                label="secondary-bds-claim-0",
            )
        )

        self.assertEqual(17, nonce)
        self.assertEqual(2, node_index)
        self.assertEqual([1, 2], refresh_attempts)
        calls = runner.healthy_submission_node_index.await_args_list
        self.assertEqual({0}, calls[0].kwargs["excluded_indices"])
        self.assertEqual({0, 1}, calls[1].kwargs["excluded_indices"])

    def test_broadcast_failover_uses_broadcast_response_hash(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [self._node(0, "http://node-0")]
        runner.network = {"chain_id": "test-chain"}
        runner.next_nonce_with_rpc_failover = AsyncMock(return_value=(7, 0))
        runner.healthy_submission_node_index = AsyncMock(return_value=0)
        runner.wait_for_tx_receipt_via_healthy_node = AsyncMock(
            return_value={
                "success": True,
                "message": None,
                "execution": {"state": [], "events": [], "chi_used": 10},
                "result": {"height": "12", "index": "0", "hash": "broadcast-hash"},
            }
        )
        wallet = SimpleNamespace(public_key="wallet-0")

        with (
            patch.object(localnet_e2e.tr, "create_tx", return_value={"signed": "tx"}),
            patch.object(localnet_e2e.XianAsync, "_local_tx_hash", return_value="local-hash"),
            patch.object(
                localnet_e2e.tr,
                "broadcast_tx_wait_async",
                AsyncMock(return_value={"result": {"code": 0, "hash": "broadcast-hash"}}),
            ),
        ):
            submission = asyncio.run(
                runner.send_tx_with_broadcast_failover(
                    None,
                    wallet,
                    "currency",
                    "transfer",
                    {"amount": 1, "to": "recipient"},
                    preferred_index=0,
                    excluded_indices=None,
                    chi=localnet_e2e.DEFAULT_TRANSFER_CHI,
                    label="transfer",
                    timeout_seconds=30,
                )
            )

        self.assertEqual("broadcast-hash", submission.tx_hash)
        receipt_call = runner.wait_for_tx_receipt_via_healthy_node.await_args
        self.assertEqual("broadcast-hash", receipt_call.args[2])

    def test_recover_lagging_nodes_restarts_abci_unresponsive_node(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
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
                raise localnet_e2e.E2EError("ABCI query timed out")

        with (
            patch.object(localnet_e2e, "wait_for_height", side_effect=fake_wait_for_height),
            patch.object(
                localnet_e2e,
                "wait_for_abci_query_responsive",
                side_effect=fake_wait_for_abci,
            ),
        ):
            result = asyncio.run(
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

    def test_governance_approval_can_use_custom_status_readers(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        send_labels = []
        status_reads = []

        class FakeClient:
            async def send_tx(self, contract, function, kwargs, **options):
                send_labels.append((contract, function, kwargs, options))
                return SimpleNamespace(
                    submitted=True,
                    accepted=True,
                    finalized=True,
                    message=None,
                    tx_hash=f"tx-{function}-{len(send_labels)}",
                    mode="checktx",
                    nonce=len(send_labels),
                    chi_supplied=options["chi"],
                    receipt=SimpleNamespace(
                        success=True,
                        message="ok",
                        execution={"state": [], "events": [], "chi_used": 10},
                    ),
                )

            async def get_state(self, *_args, **_kwargs):
                raise AssertionError("custom proposal_count_reader should be used")

            async def call(self, *_args, **_kwargs):
                raise AssertionError("custom proposal_status_reader should be used")

        async def proposal_count_reader():
            return 7

        async def proposal_status_reader(proposal_id: int):
            status_reads.append(proposal_id)
            if len(status_reads) >= 4:
                return {"status": "executed"}
            return {"status": "pending"}

        result = asyncio.run(
            runner.approve_governance_proposal(
                FakeClient(),
                [("node1", FakeClient())],
                proposal_function="propose_contract_call",
                proposal_kwargs={"target_contract": "demo"},
                expected_final_status="executed",
                label_prefix="demo",
                proposal_count_reader=proposal_count_reader,
                proposal_status_reader=proposal_status_reader,
            )
        )

        self.assertEqual(7, result["proposal_id"])
        self.assertEqual("executed", result["proposal_final"]["status"])
        self.assertEqual(
            [("governance", "propose_contract_call"), ("governance", "vote")],
            [(contract, function) for contract, function, *_rest in send_labels],
        )
        self.assertEqual(["async", "async"], [options["mode"] for *_args, options in send_labels])

    def test_members_vote_approval_uses_async_broadcasts(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        send_labels = []
        vote_status_reads = 0

        class FakeClient:
            async def send_tx(self, contract, function, kwargs, **options):
                send_labels.append((contract, function, kwargs, options))
                return SimpleNamespace(
                    submitted=True,
                    accepted=True,
                    finalized=True,
                    message=None,
                    tx_hash=f"tx-{function}-{len(send_labels)}",
                    mode=options["mode"],
                    nonce=len(send_labels),
                    chi_supplied=options["chi"],
                    receipt=SimpleNamespace(
                        success=True,
                        message="ok",
                        execution={"state": [], "events": [], "chi_used": 10},
                    ),
                )

            async def get_state(self, contract, variable, *keys):
                nonlocal vote_status_reads
                if (contract, variable) == ("validators", "total_votes"):
                    return 11
                if (contract, variable) == ("validators", "votes"):
                    vote_status_reads += 1
                    if vote_status_reads >= 4:
                        return {"status": "approved"}
                    return {"status": "pending"}
                raise AssertionError((contract, variable, keys))

        result = asyncio.run(
            runner.approve_members_vote(
                FakeClient(),
                [("node1", FakeClient())],
                type_of_vote="add_member",
                arg="member-key",
                label_prefix="member",
            )
        )

        self.assertEqual(11, result["proposal_id"])
        self.assertEqual("approved", result["proposal_final"]["status"])
        self.assertEqual(
            [("validators", "propose_vote"), ("validators", "vote")],
            [(contract, function) for contract, function, *_rest in send_labels],
        )
        self.assertEqual(["async", "async"], [options["mode"] for *_args, options in send_labels])

    def test_uniform_state_wait_requires_stable_nodes_before_retry(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        runner.stabilize_nodes = AsyncMock(
            return_value={"recovery": {"lagging": [], "restarts": []}}
        )

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

    def test_uniform_state_wait_extends_deadline_after_stability_wait(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        now = 0.0

        async def stabilize(*_args, **_kwargs):
            nonlocal now
            now = 11.0
            return {"recovery": {"lagging": [], "restarts": []}}

        runner.stabilize_nodes = AsyncMock(side_effect=stabilize)

        with (
            patch.object(localnet_e2e.time, "monotonic", side_effect=lambda: now),
            patch.object(
                localnet_e2e,
                "wait_for_uniform_node_state",
                AsyncMock(
                    side_effect=[
                        localnet_e2e.E2EError("node-1 timed out"),
                        {"node-0": "ready", "node-1": "ready"},
                    ]
                ),
            ) as wait_for_state,
        ):
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
        runner.stabilize_nodes.assert_awaited_once()
        self.assertEqual(2, wait_for_state.await_count)

    def test_fund_wallets_uses_broadcast_failover(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [self._node(0, "http://node-0")]
        runner.founder_wallet = SimpleNamespace(public_key="founder")
        runner.network = {"chain_id": "test-chain"}
        runner.stabilize_nodes = AsyncMock(return_value={"recovery": {"restarts": []}})
        runner.wait_for_uniform_node_state = AsyncMock(return_value={"node-0": "3000"})
        runner.send_tx_with_broadcast_failover = AsyncMock(
            return_value=SimpleNamespace(
                submitted=True,
                accepted=True,
                finalized=True,
                message=None,
                tx_hash="tx-hash",
                mode="checktx-failover",
                nonce=1,
                chi_supplied=localnet_e2e.DEFAULT_TRANSFER_CHI,
                receipt=SimpleNamespace(
                    success=True,
                    message="ok",
                    execution={"state": [], "events": [], "chi_used": 10},
                ),
            )
        )
        wallet = SimpleNamespace(public_key="wallet-0")

        with patch.object(localnet_e2e, "fetch_abci_query", AsyncMock(return_value="0")):
            receipts = asyncio.run(runner.fund_wallets(None, [wallet], amount=3_000))

        self.assertEqual(1, len(receipts))
        runner.send_tx_with_broadcast_failover.assert_awaited_once_with(
            None,
            runner.founder_wallet,
            "currency",
            "transfer",
            {"amount": 3000, "to": "wallet-0"},
            preferred_index=0,
            excluded_indices=None,
            chi=localnet_e2e.DEFAULT_TRANSFER_CHI,
            label="fund wallet-0 (+3000)",
            timeout_seconds=30.0,
        )

    def test_fund_wallets_uses_fixed_decimal_for_fractional_topup(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.nodes = [self._node(0, "http://node-0")]
        runner.founder_wallet = SimpleNamespace(public_key="founder")
        runner.network = {"chain_id": "test-chain"}
        runner.stabilize_nodes = AsyncMock(return_value={"recovery": {"restarts": []}})
        runner.wait_for_uniform_node_state = AsyncMock(return_value={"node-0": "3"})
        runner.send_tx_with_broadcast_failover = AsyncMock(
            return_value=SimpleNamespace(
                submitted=True,
                accepted=True,
                finalized=True,
                message=None,
                tx_hash="tx-hash",
                mode="checktx-failover",
                nonce=1,
                chi_supplied=localnet_e2e.DEFAULT_TRANSFER_CHI,
                receipt=SimpleNamespace(
                    success=True,
                    message="ok",
                    execution={"state": [], "events": [], "chi_used": 10},
                ),
            )
        )
        wallet = SimpleNamespace(public_key="wallet-0")

        with patch.object(
            localnet_e2e,
            "fetch_abci_query",
            AsyncMock(return_value={"__fixed__": "0.5"}),
        ):
            receipts = asyncio.run(runner.fund_wallets(None, [wallet], amount=3))

        self.assertEqual(1, len(receipts))
        call = runner.send_tx_with_broadcast_failover.await_args
        self.assertIsNotNone(call)
        kwargs = call.args[4]
        self.assertIsInstance(kwargs["amount"], localnet_e2e.ContractingDecimal)
        self.assertEqual("2.5", str(kwargs["amount"]))
        self.assertEqual("wallet-0", kwargs["to"])
        self.assertEqual("fund wallet-0 (+2.5)", call.kwargs["label"])

    def test_submit_contract_with_broadcast_failover_uses_submission_contract(self) -> None:
        args = localnet_e2e.build_parser().parse_args(["--rpc-timeout-seconds", "30"])
        with tempfile.TemporaryDirectory() as tmpdir:
            args.resume_dir = tmpdir
            runner = localnet_e2e.E2ERunner(args)
        runner.send_tx_with_broadcast_failover = AsyncMock(return_value="submission")
        wallet = SimpleNamespace(public_key="wallet-0")

        result = asyncio.run(
            runner.submit_contract_with_broadcast_failover(
                None,
                wallet,
                name="con_demo",
                deployment_artifacts={"module": "demo"},
                args={"owner": "wallet-0"},
                preferred_index=2,
                excluded_indices={0},
                chi=123,
                label="deploy-demo",
                timeout_seconds=45,
            )
        )

        self.assertEqual("submission", result)
        runner.send_tx_with_broadcast_failover.assert_awaited_once_with(
            None,
            wallet,
            "submission",
            "submit_contract",
            {
                "name": "con_demo",
                "deployment_artifacts": {"module": "demo"},
                "constructor_args": {"owner": "wallet-0"},
            },
            preferred_index=2,
            excluded_indices={0},
            chi=123,
            label="deploy-demo",
            timeout_seconds=45,
        )

    def test_find_matching_log_lines_keeps_tail_matches(self) -> None:
        text = "\n".join(
            [
                "INFO startup",
                "DEBUG stage=execute_tx tx=1",
                "DEBUG unrelated",
                "DEBUG stage=execute_tx tx=2",
                "TRACE stage=finalize_tx_result tx=2",
            ]
        )

        matches = localnet_e2e.find_matching_log_lines(
            text,
            lambda line: "stage=execute_tx" in line,
            limit=1,
        )

        self.assertEqual(["DEBUG stage=execute_tx tx=2"], matches)

    def test_wait_for_log_matches_waits_until_every_node_has_a_match(self) -> None:
        nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]
        observed = [
            {"node-0": ["DEBUG stage=execute_tx"], "node-1": []},
            {
                "node-0": ["DEBUG stage=execute_tx"],
                "node-1": ["DEBUG stage=execute_tx"],
            },
        ]

        with patch.object(
            localnet_e2e,
            "collect_log_matches",
            side_effect=lambda *_args, **_kwargs: observed.pop(0),
        ):
            matches = asyncio.run(
                localnet_e2e.wait_for_log_matches(
                    nodes,
                    {},
                    lambda _line: True,
                    label="DEBUG stage=execute_tx",
                    timeout_seconds=1.0,
                    poll_interval_seconds=0.0,
                )
            )

        self.assertEqual(["DEBUG stage=execute_tx"], matches["node-1"])

    def test_wait_for_log_matches_reports_missing_nodes(self) -> None:
        nodes = [
            self._node(0, "http://node-0"),
            self._node(1, "http://node-1"),
        ]

        with (
            patch.object(
                localnet_e2e,
                "collect_log_matches",
                return_value={"node-0": ["TRACE stage=finalize_tx_result"], "node-1": []},
            ),
            self.assertRaisesRegex(localnet_e2e.E2EError, "node-1"),
        ):
            asyncio.run(
                localnet_e2e.wait_for_log_matches(
                    nodes,
                    {},
                    lambda _line: True,
                    label="TRACE stage=finalize_tx_result",
                    timeout_seconds=0.0,
                    poll_interval_seconds=0.0,
                )
            )


if __name__ == "__main__":
    unittest.main()
