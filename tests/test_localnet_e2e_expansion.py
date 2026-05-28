from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

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
            {
                phase.name
                for phase in localnet_e2e_phases.PHASE_SPECS
                if not phase.uses_session
            },
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


if __name__ == "__main__":
    unittest.main()
