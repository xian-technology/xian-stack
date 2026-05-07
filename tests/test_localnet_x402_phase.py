from __future__ import annotations

import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "localnet-e2e.py"
SPEC = importlib.util.spec_from_file_location("localnet_e2e", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load {MODULE_PATH}")
localnet_e2e = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = localnet_e2e
SPEC.loader.exec_module(localnet_e2e)


class LocalnetX402PhaseTests(unittest.TestCase):
    def test_x402_phase_is_part_of_five_node_e2e_sequence(self) -> None:
        phase_names = localnet_e2e.E2ERunner.phase_names()
        x402_index = phase_names.index("03-x402-exact")

        self.assertEqual("03-contract-orchestration", phase_names[x402_index - 1])
        self.assertEqual("04-periodic-load", phase_names[x402_index + 1])

    def test_x402_phase_inputs_are_wired_for_runner_and_cli(self) -> None:
        self.assertTrue(localnet_e2e.X402_CONTRACT_SOURCE.exists())
        self.assertEqual(Decimal("0.001"), localnet_e2e.X402_PAYMENT_AMOUNT)

        args = localnet_e2e.build_parser().parse_args(
            [
                "--start-phase",
                "03-x402-exact",
                "--resume-dir",
                "/tmp/localnet-e2e",
                "--no-bootstrap",
                "--no-build",
            ]
        )

        self.assertEqual("03-x402-exact", args.start_phase)
        self.assertEqual("/tmp/localnet-e2e", args.resume_dir)


if __name__ == "__main__":
    unittest.main()
